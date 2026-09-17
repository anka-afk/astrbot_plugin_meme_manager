"""Lsky Pro open-source 2.x adapter using reversible, scoped original names."""

import base64
import hashlib
import mimetypes
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from ..core.file_handler import (
    MAX_IMAGE_BYTES,
    normalize_relative_path,
    save_image_stream,
)
from ..interfaces.image_host import ImageHostInterface, ImageInfo


class LskyProvider(ImageHostInterface):
    """Keep unrelated account images outside the synchronization namespace."""

    def __init__(self, config: dict):
        self.local_dir = Path(config["local_dir"]).resolve()
        self.base_url = str(config.get("url") or "").strip().rstrip("/")
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Lsky requires an HTTP(S) site URL without credentials or query parameters"
            )
        token = str(config.get("token") or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token or any(c in token for c in "\r\n"):
            raise ValueError("Lsky requires an API token")
        self.namespace = str(config.get("namespace") or "memes").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.namespace):
            raise ValueError(
                "Lsky namespace must contain 1-32 ASCII letters, digits, underscores or hyphens"
            )
        self.prefix = f"amm1_{len(self.namespace)}_{self.namespace}__"
        self.strategy_id = int(config.get("strategy_id") or 0)
        if self.strategy_id < 0:
            raise ValueError("Lsky strategy ID cannot be negative")
        self.session = requests.Session()
        self._inventory = None
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Perform one API operation without replaying ambiguous uploads.

        Args:
            method: HTTP method.
            path: Relative API route.
            **kwargs: Request body or query parameters.

        Returns:
            Successful response data.

        Raises:
            ValueError: Authentication, transport or response validation failed.
        """
        try:
            with self.session.request(
                method,
                f"{self.base_url}/api/v1/{path}",
                timeout=(10, 60),
                allow_redirects=False,
                **kwargs,
            ) as response:
                if response.status_code != 200:
                    raise ValueError(f"Lsky API failed: HTTP {response.status_code}")
                result = response.json()
                if not isinstance(result, dict) or result.get("status") is not True:
                    raise ValueError(
                        "Lsky rejected the operation; check API permissions, quota and image policy"
                    )
                return result.get("data") or {}
        except requests.RequestException as exc:
            raise ValueError(
                f"Lsky API transport failed ({type(exc).__name__})"
            ) from None

    def get_image_list(self) -> list[ImageInfo]:
        """Traverse all albums because uploads use the account's default album.

        Returns:
            Complete inventory of images in this plugin namespace.

        Raises:
            ValueError: Pagination, managed metadata or paths are invalid.
        """
        self._inventory = None
        images = []
        album_ids = [0]
        seen_keys = set()
        seen_paths = set()
        # The album pass populates subsequent image queries, including unfiled images.
        queries = [("albums", {})]
        for route, filters in queries:
            total = None
            seen = set()
            for page in range(1, 10001):
                data = self._request("GET", route, params={**filters, "page": page})
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise ValueError("Lsky returned an invalid listing")
                current_total, last = data.get("total"), data.get("last_page")
                if (
                    type(current_total) is not int
                    or current_total < 0
                    or type(last) is not int
                    or not page <= last <= 10000
                    or data.get("current_page") != page
                    or (total is not None and total != current_total)
                ):
                    raise ValueError("Lsky pagination changed or is incomplete")
                total = current_total
                for source in data["data"]:
                    identity = (
                        source.get("id") if route == "albums" else source.get("key")
                    )
                    if not identity or identity in seen:
                        raise ValueError(
                            "Lsky listing contains missing or repeated identifiers"
                        )
                    seen.add(identity)
                    if route == "albums":
                        if type(identity) is not int or identity <= 0:
                            raise ValueError("Lsky returned an invalid album ID")
                        album_ids.append(identity)
                        continue
                    if identity in seen_keys:
                        raise ValueError(
                            "Lsky image moved between albums during listing"
                        )
                    seen_keys.add(identity)
                    name = str(source.get("origin_name") or "")
                    if not name.startswith(self.prefix):
                        continue
                    if not re.fullmatch(r"[A-Za-z0-9]+", str(identity)):
                        raise ValueError("Lsky returned an invalid image key")
                    encoded, _, extension = name[len(self.prefix) :].rpartition(".")
                    try:
                        relative = normalize_relative_path(
                            base64.b64decode(
                                encoded + "=" * (-len(encoded) % 4),
                                altchars=b"-_",
                                validate=True,
                            ).decode("utf-8")
                        )
                    except (ValueError, UnicodeError) as exc:
                        raise ValueError("Lsky managed image name is invalid") from exc
                    if (
                        base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")
                        != encoded
                        or Path(relative).suffix.lower() != f".{extension}".lower()
                    ):
                        raise ValueError(
                            "Lsky changed a managed image filename; disable format conversion"
                        )
                    if relative.casefold() in seen_paths:
                        raise ValueError(
                            "Lsky contains duplicate managed paths; remove the extra copy before syncing"
                        )
                    seen_paths.add(relative.casefold())
                    sha1 = str(source.get("sha1") or "")
                    if not re.fullmatch(r"[0-9a-f]{40}", sha1):
                        raise ValueError("Lsky did not provide an image checksum")
                    category, _, filename = relative.rpartition("/")
                    images.append(
                        {
                            "id": identity,
                            "relative_path": relative,
                            "filename": filename,
                            "category": category,
                            "url": (source.get("links") or {}).get("url", ""),
                            # Lsky stores rounded KiB, not an exact byte count.
                            "size": None,
                            "etag": f"{identity}:{sha1}",
                            "modified": str(source.get("date") or ""),
                        }
                    )
                if page == last:
                    if len(seen) != total:
                        raise ValueError("Lsky returned an incomplete listing")
                    break
                if not data["data"]:
                    raise ValueError("Lsky listing ended before its last page")
            if route == "albums":
                queries.extend(
                    ("images", {"album_id": album_id}) for album_id in album_ids
                )
        self._inventory = images
        return images

    def upload_image(self, file_path: Path) -> ImageInfo:
        """Upload and verify a replacement before deleting its previous record.

        Args:
            file_path: Image under the configured local root.

        Returns:
            Verified remote metadata.

        Raises:
            ValueError: The server transformed the image or returned invalid metadata.
        """
        relative = normalize_relative_path(
            file_path.resolve().relative_to(self.local_dir).as_posix()
        )
        encoded = base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")
        name = self.prefix + encoded + file_path.suffix.lower()
        if len(name) > 255:
            raise ValueError(
                "Lsky encoded filename exceeds 255 bytes; shorten the category or filename"
            )
        inventory = (
            self.get_image_list() if self._inventory is None else self._inventory
        )
        existing = [image for image in inventory if image["relative_path"] == relative]
        with file_path.open("rb") as stream:
            content = stream.read(MAX_IMAGE_BYTES + 1)
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise ValueError("Lsky upload exceeds the supported image size")
        sha1 = hashlib.sha1(content).hexdigest()
        if existing and existing[0]["etag"].endswith(f":{sha1}"):
            return {**existing[0], "sha256": hashlib.sha256(content).hexdigest()}
        self._inventory = None
        data = self._request(
            "POST",
            "upload",
            data={"strategy_id": self.strategy_id} if self.strategy_id else {},
            files={
                "file": (
                    name,
                    content,
                    mimetypes.guess_type(name)[0] or "application/octet-stream",
                )
            },
        )
        key = str(data.get("key") or "")
        if not re.fullmatch(r"[A-Za-z0-9]+", key) or any(
            image["id"] == key for image in existing
        ):
            raise ValueError("Lsky did not return a new image key")
        if data.get("origin_name") != name or data.get("sha1") != sha1:
            # Keep old content intact; only roll back the new record returned by upload.
            self.delete_image(key)
            raise ValueError(
                "Lsky changed the image; disable compression, format conversion and overlay watermarks"
            )
        for image in existing:
            self.delete_image(image["id"])
        category, _, filename = relative.rpartition("/")
        uploaded = {
            "id": key,
            "relative_path": relative,
            "filename": filename,
            "category": category,
            "url": (data.get("links") or {}).get("url", ""),
            "size": None,
            "sha256": hashlib.sha256(content).hexdigest(),
            "etag": f"{key}:{sha1}",
        }
        self._inventory = [image for image in inventory if image not in existing] + [
            uploaded
        ]
        return uploaded

    def delete_image(self, image_id: str) -> bool:
        """Delete an opaque key returned by a managed listing or upload.

        Args:
            image_id: Lsky image key.

        Returns:
            True after confirmation from the server.
        """
        if not re.fullmatch(r"[A-Za-z0-9]+", image_id):
            raise ValueError("Invalid Lsky image key")
        self._request("DELETE", f"images/{quote(image_id, safe='')}")
        self._inventory = None
        return True

    def download_image(self, image_info: ImageInfo, save_path: Path) -> bool:
        """Validate original bytes before publishing, without sending API credentials.

        Args:
            image_info: Managed remote metadata.
            save_path: Local destination.

        Returns:
            True after checksum verification and atomic replacement.
        """
        url = str(image_info.get("url") or "")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Lsky did not return a usable original image URL")
        expected = str(image_info.get("etag") or "").rpartition(":")[2]
        if not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise ValueError("Lsky download is missing a checksum")
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".lsky-download-", dir=save_path.parent
        ) as directory:
            temporary = Path(directory) / "image"
            with requests.get(url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                size = response.headers.get("Content-Length", "")
                save_image_stream(
                    response.iter_content(1024 * 1024),
                    temporary,
                    int(size)
                    if size.isdigit() and not response.headers.get("Content-Encoding")
                    else None,
                )
            digest = hashlib.sha1()
            with temporary.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(
                    "Lsky downloaded image checksum mismatch; check original image access and watermark settings"
                )
            temporary.replace(save_path)
        return True

    def close(self) -> None:
        """Release the API connection pool."""
        self.session.close()
