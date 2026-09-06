"""WebDAV adapter with complete traversal and strict namespace boundaries."""

import mimetypes
import posixpath
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..core.file_handler import (
    SUPPORTED_FORMATS,
    normalize_relative_path,
    save_image_stream,
)
from ..interfaces.image_host import ImageHostInterface, ImageInfo


class WebDAVError(Exception):
    """WebDAV adapter error."""


class AuthenticationError(WebDAVError):
    """WebDAV rejected the configured credentials."""


class NetworkError(WebDAVError):
    """A WebDAV request could not be completed."""


class InvalidResponseError(WebDAVError):
    """The server did not return a complete, valid WebDAV response."""


class WebDAVProvider(ImageHostInterface):
    """Use namespace-relative IDs for WebDAV objects."""

    SUPPORTED_EXTENSIONS = SUPPORTED_FORMATS

    def __init__(self, config: dict):
        missing = [
            field for field in ("url", "username", "password") if not config.get(field)
        ]
        if missing:
            raise ValueError(f"Missing WebDAV configuration: {', '.join(missing)}")
        self.config = dict(config)
        self.base_url = str(config["url"]).rstrip("/")
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "WebDAV URL must be an HTTP(S) endpoint without query parameters"
            )
        self.base_path = self._normalize_path(config.get("base_path", "memes"))
        self.public_url = str(config.get("public_url") or "").rstrip("/")
        self.local_dir = Path(config["local_dir"]).resolve()
        self.timeout = max(1, min(int(config.get("timeout") or 30), 300))
        self.verify_ssl = self._parse_bool(config.get("verify_ssl", True))
        self.session = requests.Session()
        self.session.auth = (config["username"], config["password"])
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods={"GET", "HEAD", "PUT", "DELETE", "MKCOL", "PROPFIND"},
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def upload_image(self, file_path: Path) -> ImageInfo:
        """Upload a file under its complete relative category.

        Args:
            file_path: Image within the local root.

        Returns:
            Confirmed remote metadata.
        """
        relative = self._get_remote_id(file_path)
        remote_path = self._remote_id_to_path(relative)
        self._ensure_remote_dirs(posixpath.dirname(remote_path))
        with file_path.open("rb") as stream:
            response = self._request(
                "PUT",
                self._url_for_path(remote_path),
                data=stream,
                headers={
                    "Content-Type": mimetypes.guess_type(file_path.name)[0]
                    or "application/octet-stream"
                },
            )
        try:
            if response.status_code not in (200, 201, 204):
                raise InvalidResponseError(
                    f"WebDAV upload failed: HTTP {response.status_code}"
                )
            category, _, filename = relative.rpartition("/")
            return {
                "id": relative,
                "relative_path": relative,
                "filename": filename,
                "category": category,
                "url": self._public_url_for_id(relative),
                "size": file_path.stat().st_size,
                "etag": response.headers.get("ETag", ""),
            }
        finally:
            response.close()

    def delete_image(self, image_id: str) -> bool:
        """Delete the exact relative ID; never strip a legitimate category prefix.

        Args:
            image_id: Relative ID returned by this adapter.

        Returns:
            Whether the file is absent after the request.
        """
        response = self._request(
            "DELETE", self._url_for_path(self._remote_id_to_path(image_id))
        )
        try:
            return response.status_code in (200, 204, 404)
        finally:
            response.close()

    def get_image_list(self) -> list[ImageInfo]:
        """Traverse Depth: 1 listings without creating remote directories.

        Returns:
            A complete list of images.

        Raises:
            InvalidResponseError: Any subtree is inaccessible or outside the root.
        """
        images = []
        pending = [self.base_path]
        visited = set()
        self.listing_exists = True
        while pending:
            remote_dir = pending.pop()
            if remote_dir in visited or len(visited) >= 10000:
                raise InvalidResponseError(
                    "WebDAV traversal is cyclic or exceeds its directory limit"
                )
            visited.add(remote_dir)
            response = self._propfind(remote_dir, depth=1)
            try:
                if response.status_code == 404 and remote_dir == self.base_path:
                    self.listing_exists = False
                    return []
                entries = self._parse_propfind_response(response.text, remote_dir)
            finally:
                response.close()
            for entry in entries:
                path = entry["path"]
                if path == remote_dir:
                    continue
                if posixpath.dirname(path) != remote_dir:
                    raise InvalidResponseError(
                        "WebDAV returned an entry outside the requested directory"
                    )
                if entry["is_dir"]:
                    pending.append(path)
                    continue
                filename = posixpath.basename(path)
                if Path(filename).suffix.lower() not in SUPPORTED_FORMATS:
                    continue
                relative = self._strip_base_path(path)
                category, _, filename = relative.rpartition("/")
                images.append(
                    {
                        "id": relative,
                        "relative_path": relative,
                        "filename": filename,
                        "category": category,
                        "url": self._public_url_for_id(relative),
                        "size": entry["size"],
                        "etag": entry["etag"],
                        "modified": entry["modified"],
                    }
                )
        return images

    def download_image(self, image_info: ImageInfo, save_path: Path) -> bool:
        """Stream a conditional GET into a validated atomic image write.

        Args:
            image_info: Remote metadata.
            save_path: Local destination.

        Returns:
            True when the image is complete and validated.
        """
        headers = {"If-Match": image_info["etag"]} if image_info.get("etag") else {}
        response = self._request(
            "GET",
            self._url_for_path(self._remote_id_to_path(image_info["id"])),
            headers=headers,
            stream=True,
        )
        try:
            if response.status_code != 200:
                raise InvalidResponseError(
                    f"WebDAV download failed: HTTP {response.status_code}"
                )
            size = image_info.get("size")
            if size is None and response.headers.get("Content-Length", "").isdigit():
                size = int(response.headers["Content-Length"])
            save_image_stream(
                response.iter_content(chunk_size=1024 * 1024),
                Path(save_path),
                size,
                image_info.get("sha256", ""),
            )
        finally:
            response.close()
        return True

    def _propfind(self, remote_path: str, depth: int = 1) -> requests.Response:
        """Request image metadata without modifying the remote filesystem.

        Args:
            remote_path: Directory to list.
            depth: WebDAV traversal depth.

        Returns:
            Response to parse and close.
        """
        response = self._request(
            "PROPFIND",
            self._url_for_path(remote_path),
            headers={"Depth": str(depth), "Content-Type": "application/xml"},
            data=b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop>'
            b"<D:resourcetype/><D:getcontentlength/><D:getetag/><D:getlastmodified/>"
            b"</D:prop></D:propfind>",
        )
        if response.status_code not in (200, 207) and not (
            response.status_code == 404 and remote_path == self.base_path
        ):
            status = response.status_code
            response.close()
            raise InvalidResponseError(f"WebDAV listing failed: HTTP {status}")
        return response

    def _parse_propfind_response(
        self, xml_text: str, requested_path: str
    ) -> list[dict]:
        """Require successful resource types and preserve optional metadata.

        Args:
            xml_text: WebDAV multistatus XML.
            requested_path: Directory whose immediate children were requested.

        Returns:
            Validated entries.

        Raises:
            InvalidResponseError: The response is malformed or incomplete.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise InvalidResponseError("Invalid WebDAV XML") from exc
        if root.tag != "{DAV:}multistatus":
            raise InvalidResponseError("WebDAV response is not a multistatus listing")
        entries = []
        for response in root.findall("{DAV:}response"):
            href = response.findtext("{DAV:}href")
            if not href:
                raise InvalidResponseError("WebDAV entry is missing its href")
            props = {}
            for propstat in response.findall("{DAV:}propstat"):
                status = propstat.findtext("{DAV:}status", "")
                if " 200 " not in f"{status} ":
                    continue
                prop = propstat.find("{DAV:}prop")
                if prop is not None:
                    props.update({child.tag: child for child in prop})
            resource_type = props.get("{DAV:}resourcetype")
            if resource_type is None:
                raise InvalidResponseError(
                    "WebDAV entry has no successful resource type"
                )
            size_element = props.get("{DAV:}getcontentlength")
            size_text = size_element.text if size_element is not None else ""
            size = int(size_text) if size_text and size_text.isdigit() else None
            etag = props.get("{DAV:}getetag")
            modified = props.get("{DAV:}getlastmodified")
            entries.append(
                {
                    "path": self._path_from_href(href, requested_path),
                    "is_dir": resource_type.find("{DAV:}collection") is not None,
                    "size": size,
                    "etag": etag.text or "" if etag is not None else "",
                    "modified": modified.text or "" if modified is not None else "",
                }
            )
        if not entries:
            raise InvalidResponseError("WebDAV listing omitted the requested directory")
        return entries

    def _ensure_remote_dirs(self, remote_dir: str) -> None:
        """Create each directory needed for an upload.

        Args:
            remote_dir: Namespace-qualified directory path.
        """
        current = ""
        for part in remote_dir.split("/") if remote_dir else []:
            current = f"{current}/{part}" if current else part
            response = self._request("MKCOL", self._url_for_path(current))
            try:
                if response.status_code not in (201, 405):
                    raise InvalidResponseError(
                        f"WebDAV mkdir failed: HTTP {response.status_code}"
                    )
            finally:
                response.close()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Apply bounded timeouts, certificate settings and authentication checks.

        Args:
            method: HTTP method.
            url: WebDAV request URL.
            **kwargs: Request body, headers and streaming options.

        Returns:
            Response owned by the caller.

        Raises:
            AuthenticationError: Credentials were rejected.
            NetworkError: The request failed.
        """
        kwargs.setdefault("timeout", (min(10, self.timeout), self.timeout))
        kwargs.setdefault("verify", self.verify_ssl)
        kwargs.setdefault("allow_redirects", False)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise NetworkError(f"WebDAV request failed ({type(exc).__name__})") from exc
        if response.status_code in (401, 403):
            response.close()
            raise AuthenticationError("WebDAV authentication failed")
        if 300 <= response.status_code < 400:
            response.close()
            raise InvalidResponseError(
                "WebDAV redirected the request; configure the final endpoint URL"
            )
        return response

    def _url_for_path(self, remote_path: str) -> str:
        """Build an escaped request URL.

        Args:
            remote_path: Namespace-qualified path.

        Returns:
            WebDAV request URL.
        """
        remote_path = self._normalize_path(remote_path)
        return (
            f"{self.base_url}/{quote(remote_path, safe='/')}"
            if remote_path
            else self.base_url
        )

    def _public_url_for_id(self, remote_id: str) -> str:
        """Build an escaped public URL or the authenticated DAV URL.

        Args:
            remote_id: Relative image ID.

        Returns:
            Image URL.
        """
        remote_id = normalize_relative_path(remote_id)
        if self.public_url:
            return f"{self.public_url}/{quote(remote_id, safe='/')}"
        return self._url_for_path(self._remote_id_to_path(remote_id))

    def _get_remote_id(self, file_path: Path) -> str:
        """Preserve the full relative path and reject files outside the library.

        Args:
            file_path: Local image.

        Returns:
            Portable relative image ID.
        """
        return normalize_relative_path(
            file_path.resolve().relative_to(self.local_dir).as_posix()
        )

    def _remote_id_to_path(self, remote_id: str) -> str:
        """Attach the configured remote namespace exactly once.

        Args:
            remote_id: Relative image ID.

        Returns:
            Namespace-qualified path.
        """
        relative = normalize_relative_path(remote_id)
        return f"{self.base_path}/{relative}" if self.base_path else relative

    def _strip_base_path(self, remote_path: str) -> str:
        """Remove the namespace from a validated listing path.

        Args:
            remote_path: Namespace-qualified path.

        Returns:
            Relative path.

        Raises:
            ValueError: The listing escaped the configured namespace.
        """
        if remote_path == self.base_path:
            return ""
        if not self.base_path:
            return normalize_relative_path(remote_path)
        prefix = f"{self.base_path}/"
        if not remote_path.startswith(prefix):
            raise ValueError("WebDAV entry is outside the configured namespace")
        return normalize_relative_path(remote_path[len(prefix) :])

    def _path_from_href(self, href: str, requested_path: str) -> str:
        """Resolve DAV hrefs with exact origin and URL path boundaries.

        Args:
            href: Entry URL or relative href.
            requested_path: Directory being listed.

        Returns:
            Validated namespace-qualified path.

        Raises:
            InvalidResponseError: The href points outside the configured endpoint.
        """
        endpoint = urlparse(self.base_url)
        resolved = urlparse(
            urljoin(self._url_for_path(requested_path).rstrip("/") + "/", href)
        )
        if (resolved.scheme, resolved.netloc) != (endpoint.scheme, endpoint.netloc):
            raise InvalidResponseError("WebDAV href points to a different origin")
        path = unquote(resolved.path).rstrip("/")
        base = unquote(endpoint.path).rstrip("/")
        if base and path != base and not path.startswith(base + "/"):
            raise InvalidResponseError("WebDAV href escapes the endpoint path")
        path = path[len(base) :].lstrip("/")
        if not path and not self.base_path:
            return ""
        return normalize_relative_path(path)

    def _normalize_path(self, value: str | Path | None) -> str:
        """Validate a configured DAV path.

        Args:
            value: Path with optional leading and trailing slashes.

        Returns:
            Normalized path, or the DAV root.
        """
        value = str(value or "").replace("\\", "/").strip("/")
        return normalize_relative_path(value) if value else ""

    @staticmethod
    def _parse_bool(value) -> bool:
        """Interpret configuration boolean values.

        Args:
            value: Boolean or textual configuration value.

        Returns:
            Parsed boolean.
        """
        return (
            value.strip().lower() not in {"0", "false", "no", "off", "否"}
            if isinstance(value, str)
            else bool(value)
        )

    def close(self) -> None:
        """Close the authenticated HTTP session."""
        self.session.close()
