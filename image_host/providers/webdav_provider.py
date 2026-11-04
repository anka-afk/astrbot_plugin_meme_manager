"""WebDAV 适配器，完整遍历并严格校验命名空间边界。"""

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
    """WebDAV 适配器错误。"""


class AuthenticationError(WebDAVError):
    """WebDAV 拒绝了配置的凭据。"""


class NetworkError(WebDAVError):
    """WebDAV 请求无法完成。"""


class InvalidResponseError(WebDAVError):
    """服务器未返回完整有效的 WebDAV 响应。"""


class WebDAVProvider(ImageHostInterface):
    """为 WebDAV 对象使用相对于命名空间的 ID。"""

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
        """在完整相对分类下上传文件。

        Args:
            file_path: 本地根目录内的图片。

        Returns:
            已确认的远端元数据。
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
        """删除确切的相对 ID，绝不去掉有效的分类前缀。

        Args:
            image_id: 此适配器返回的相对 ID。

        Returns:
            请求完成后文件是否已不存在。
        """
        response = self._request(
            "DELETE", self._url_for_path(self._remote_id_to_path(image_id))
        )
        try:
            return response.status_code in (200, 204, 404)
        finally:
            response.close()

    def get_image_list(self) -> list[ImageInfo]:
        """遍历 Depth: 1 清单，不创建远端目录。

        Returns:
            完整的图片列表。

        Raises:
            InvalidResponseError: 任一子树不可访问或超出根目录。
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
        """将条件 GET 响应流式写入经过校验的原子图片写入流程。

        Args:
            image_info: 远端元数据。
            save_path: 本地目标路径。

        Returns:
            图片完整且通过校验时返回 True。
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
        """请求图片元数据，不修改远端文件系统。

        Args:
            remote_path: 待列出的目录。
            depth: WebDAV 遍历深度。

        Returns:
            待解析并关闭的响应。
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
        """要求资源类型状态成功，并保留可选元数据。

        Args:
            xml_text: WebDAV 多状态 XML。
            requested_path: 请求直接子项的目录。

        Returns:
            已校验的条目。

        Raises:
            InvalidResponseError: 响应格式错误或不完整。
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
        """创建上传所需的各级目录。

        Args:
            remote_dir: 包含命名空间的目录路径。
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
        """应用有界超时、证书设置与身份验证检查。

        Args:
            method: HTTP 方法。
            url: WebDAV 请求 URL。
            **kwargs: 请求体、请求头及流式选项。

        Returns:
            由调用方负责管理的响应。

        Raises:
            AuthenticationError: 凭据被拒绝。
            NetworkError: 请求失败。
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
        """构造经过转义的请求 URL。

        Args:
            remote_path: 包含命名空间的路径。

        Returns:
            WebDAV 请求 URL。
        """
        remote_path = self._normalize_path(remote_path)
        return (
            f"{self.base_url}/{quote(remote_path, safe='/')}"
            if remote_path
            else self.base_url
        )

    def _public_url_for_id(self, remote_id: str) -> str:
        """构造经过转义的公开 URL 或需身份验证的 DAV URL。

        Args:
            remote_id: 相对图片 ID。

        Returns:
            图片 URL。
        """
        remote_id = normalize_relative_path(remote_id)
        if self.public_url:
            return f"{self.public_url}/{quote(remote_id, safe='/')}"
        return self._url_for_path(self._remote_id_to_path(remote_id))

    def _get_remote_id(self, file_path: Path) -> str:
        """保留完整相对路径，并拒绝图片库之外的文件。

        Args:
            file_path: 本地图片。

        Returns:
            可跨平台使用的相对图片 ID。
        """
        return normalize_relative_path(
            file_path.resolve().relative_to(self.local_dir).as_posix()
        )

    def _remote_id_to_path(self, remote_id: str) -> str:
        """只添加一次配置的远端命名空间。

        Args:
            remote_id: 相对图片 ID。

        Returns:
            包含命名空间的路径。
        """
        relative = normalize_relative_path(remote_id)
        return f"{self.base_path}/{relative}" if self.base_path else relative

    def _strip_base_path(self, remote_path: str) -> str:
        """从已校验的清单路径中移除命名空间。

        Args:
            remote_path: 包含命名空间的路径。

        Returns:
            相对路径。

        Raises:
            ValueError: 清单路径超出已配置的命名空间。
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
        """解析 DAV href，严格检查来源与 URL 路径边界。

        Args:
            href: 条目 URL 或相对 href。
            requested_path: 正在列出的目录。

        Returns:
            经过校验且包含命名空间的路径。

        Raises:
            InvalidResponseError: href 指向配置端点之外。
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
        """校验配置中的 DAV 路径。

        Args:
            value: 可带前导或尾部斜杠的路径。

        Returns:
            规范化路径，或 DAV 根目录。
        """
        value = str(value or "").replace("\\", "/").strip("/")
        return normalize_relative_path(value) if value else ""

    @staticmethod
    def _parse_bool(value) -> bool:
        """解释配置中的布尔值。

        Args:
            value: 布尔值或文本形式的配置值。

        Returns:
            解析后的布尔值。
        """
        return (
            value.strip().lower() not in {"0", "false", "no", "off", "否"}
            if isinstance(value, str)
            else bool(value)
        )

    def close(self) -> None:
        """关闭经过身份验证的 HTTP 会话。"""
        self.session.close()
