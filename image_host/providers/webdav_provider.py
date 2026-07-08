import logging
import posixpath
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict

import requests

from ..interfaces.image_host import ImageHostInterface

logger = logging.getLogger(__name__)


class WebDAVError(Exception):
    """WebDAV 图床基础异常"""


class AuthenticationError(WebDAVError):
    """认证失败异常"""


class NetworkError(WebDAVError):
    """网络请求失败异常"""


class InvalidResponseError(WebDAVError):
    """响应格式异常"""


class ImageInfo(TypedDict):
    id: str
    url: str
    filename: str
    category: str
    size: int


class WebDAVProvider(ImageHostInterface):
    """WebDAV 图床/云存储提供者。"""

    SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

    def __init__(self, config: dict[str, str]):
        required_fields = ["url", "username", "password"]
        missing_fields = [field for field in required_fields if not config.get(field)]
        if missing_fields:
            raise ValueError(f"WebDAV 配置缺少必要字段: {', '.join(missing_fields)}")

        self.config = config
        self.base_url = str(config["url"]).rstrip("/")
        self.username = config["username"]
        self.password = config["password"]
        self.base_path = self._normalize_path(config.get("base_path", "memes"))
        self.public_url = str(config.get("public_url", "")).rstrip("/")
        self.local_dir = (
            Path(config.get("local_dir", "")) if config.get("local_dir") else None
        )
        self.timeout = int(config.get("timeout", 30) or 30)
        self.verify_ssl = self._parse_bool(config.get("verify_ssl", True))

        self.session = requests.Session()
        self.session.auth = (self.username, self.password)

        logger.info("初始化 WebDAV 图床: %s/%s", self.base_url, self.base_path)

    def upload_image(self, file_path: Path) -> ImageInfo:
        """上传图片到 WebDAV。"""
        file_path = Path(file_path)
        remote_id = self._get_remote_id(file_path)
        remote_path = self._remote_id_to_path(remote_id)

        self._ensure_remote_dirs(posixpath.dirname(remote_path))
        with open(file_path, "rb") as file:
            response = self._request("PUT", self._url_for_path(remote_path), data=file)

        if response.status_code not in (200, 201, 204):
            raise InvalidResponseError(
                f"WebDAV 上传失败: HTTP {response.status_code} {response.text[:200]}"
            )

        category = posixpath.dirname(remote_id)
        filename = posixpath.basename(remote_id)
        return {
            "id": remote_id,
            "url": self._public_url_for_id(remote_id),
            "filename": filename,
            "category": "" if category == "." else category,
            "size": file_path.stat().st_size,
        }

    def delete_image(self, image_hash: str) -> bool:
        """从 WebDAV 删除图片。"""
        remote_id = self._strip_base_path(image_hash)
        remote_path = self._remote_id_to_path(remote_id)
        response = self._request("DELETE", self._url_for_path(remote_path))
        return response.status_code in (200, 204, 404)

    def get_image_list(self) -> list[ImageInfo]:
        """递归获取 WebDAV 上的图片列表。"""
        self._ensure_remote_dirs(self.base_path)
        images: list[ImageInfo] = []
        self._collect_images(self.base_path, images)
        return images

    def download_image(self, image_info: dict[str, str], save_path: Path) -> bool:
        """从 WebDAV 下载图片到本地。"""
        remote_id = self._strip_base_path(image_info["id"])
        remote_path = self._remote_id_to_path(remote_id)
        response = self._request("GET", self._url_for_path(remote_path), stream=True)
        if response.status_code != 200:
            return False

        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    file.write(chunk)
        return save_path.exists() and save_path.stat().st_size > 0

    def _collect_images(self, remote_dir: str, images: list[ImageInfo]) -> None:
        response = self._propfind(remote_dir, depth=1)
        entries = self._parse_propfind_response(response.text, remote_dir)

        for entry in entries:
            if entry["path"] == remote_dir:
                continue
            if entry["is_dir"]:
                self._collect_images(entry["path"], images)
                continue
            filename = posixpath.basename(entry["path"])
            if Path(filename).suffix.lower() not in self.SUPPORTED_EXTENSIONS:
                continue

            remote_id = self._strip_base_path(entry["path"])
            category = posixpath.dirname(remote_id)
            images.append(
                {
                    "id": remote_id,
                    "url": self._public_url_for_id(remote_id),
                    "filename": filename,
                    "category": "" if category == "." else category,
                    "size": entry["size"],
                }
            )

    def _propfind(self, remote_path: str, depth: int = 1) -> requests.Response:
        body = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:resourcetype />
    <D:getcontentlength />
  </D:prop>
</D:propfind>"""
        response = self._request(
            "PROPFIND",
            self._url_for_path(remote_path),
            headers={"Depth": str(depth), "Content-Type": "application/xml"},
            data=body.encode("utf-8"),
        )
        if response.status_code not in (207, 200):
            raise InvalidResponseError(
                f"WebDAV 列表失败: HTTP {response.status_code} {response.text[:200]}"
            )
        return response

    def _parse_propfind_response(
        self, xml_text: str, requested_path: str
    ) -> list[dict]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise InvalidResponseError(f"WebDAV 返回 XML 解析失败: {exc}") from exc

        entries = []
        namespace = {"d": "DAV:"}
        for response in root.findall("d:response", namespace):
            href = response.findtext("d:href", default="", namespaces=namespace)
            if not href:
                continue

            remote_path = self._path_from_href(href, requested_path)
            resource_type = response.find(".//d:resourcetype", namespace)
            is_dir = (
                resource_type is not None
                and resource_type.find("d:collection", namespace) is not None
            )
            size_text = response.findtext(
                ".//d:getcontentlength", default="0", namespaces=namespace
            )
            entries.append(
                {
                    "path": remote_path,
                    "is_dir": is_dir,
                    "size": int(size_text) if str(size_text).isdigit() else 0,
                }
            )
        return entries

    def _ensure_remote_dirs(self, remote_dir: str) -> None:
        parts = [part for part in remote_dir.split("/") if part]
        current = ""
        for part in parts:
            current = part if not current else posixpath.join(current, part)
            response = self._request("MKCOL", self._url_for_path(current))
            if response.status_code not in (201, 405):
                raise InvalidResponseError(
                    f"WebDAV 创建目录失败: {current}, HTTP {response.status_code}"
                )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify_ssl)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise NetworkError(f"WebDAV 请求失败: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthenticationError("WebDAV 认证失败，请检查用户名和密码")
        return response

    def _url_for_path(self, remote_path: str) -> str:
        quoted_parts = [
            urllib.parse.quote(part) for part in remote_path.split("/") if part
        ]
        if not quoted_parts:
            return self.base_url
        return f"{self.base_url}/{'/'.join(quoted_parts)}"

    def _public_url_for_id(self, remote_id: str) -> str:
        if not self.public_url:
            return self._url_for_path(self._remote_id_to_path(remote_id))
        quoted_parts = [
            urllib.parse.quote(part) for part in remote_id.split("/") if part
        ]
        return (
            f"{self.public_url}/{'/'.join(quoted_parts)}"
            if quoted_parts
            else self.public_url
        )

    def _get_remote_id(self, file_path: Path) -> str:
        if self.local_dir:
            try:
                relative_path = file_path.relative_to(self.local_dir)
                return self._normalize_path(str(relative_path))
            except ValueError:
                pass
        return file_path.name

    def _remote_id_to_path(self, remote_id: str) -> str:
        normalized_id = self._normalize_path(remote_id)
        return (
            posixpath.join(self.base_path, normalized_id)
            if normalized_id
            else self.base_path
        )

    def _strip_base_path(self, remote_path: str) -> str:
        normalized_path = self._normalize_path(remote_path)
        if normalized_path == self.base_path:
            return ""
        prefix = f"{self.base_path}/" if self.base_path else ""
        if prefix and normalized_path.startswith(prefix):
            return normalized_path[len(prefix) :]
        return normalized_path

    def _path_from_href(self, href: str, requested_path: str) -> str:
        parsed_href_path = urllib.parse.unquote(urllib.parse.urlparse(href).path).strip(
            "/"
        )
        base_url_path = urllib.parse.unquote(
            urllib.parse.urlparse(self.base_url).path
        ).strip("/")
        if base_url_path and parsed_href_path.startswith(base_url_path):
            parsed_href_path = parsed_href_path[len(base_url_path) :].strip("/")
        if parsed_href_path:
            return self._normalize_path(parsed_href_path)
        return self._normalize_path(requested_path)

    def _normalize_path(self, value: str | Path | None) -> str:
        if value is None:
            return ""
        normalized = str(value).replace("\\", "/").strip("/")
        return posixpath.normpath(normalized).strip("/") if normalized else ""

    def _parse_bool(self, value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", "否"}
        return bool(value)
