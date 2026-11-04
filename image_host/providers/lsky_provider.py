"""兰空图床开源版 2.x 适配器，使用可逆且限定范围的原始名称。"""

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
    """将账户中的无关图片排除在同步命名空间之外。"""

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
        """执行一次 API 操作，不重放结果不确定的上传。

        Args:
            method: HTTP 方法。
            path: 相对 API 路由。
            **kwargs: 请求体或查询参数。

        Returns:
            成功响应的数据。

        Raises:
            ValueError: 身份验证、传输或响应校验失败。
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
        """遍历全部相册，因为上传使用账户的默认相册。

        Returns:
            本插件命名空间内的完整图片清单。

        Raises:
            ValueError: 分页、受管元数据或路径无效。
        """
        self._inventory = None
        images = []
        album_ids = [0]
        seen_keys = set()
        seen_paths = set()
        # 遍历相册时也要收集后续图片查询所需的数据，包括未归档图片。
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
                            # Lsky 保存的是取整后的 KiB，而非精确字节数。
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
        """先上传并验证替代图片，再删除先前记录。

        Args:
            file_path: 已配置本地根目录下的图片。

        Returns:
            已验证的远端元数据。

        Raises:
            ValueError: 服务器修改了图片或返回无效元数据。
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
            # 保留旧内容，仅回滚上传返回的新记录。
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
        """删除受管清单或上传结果返回的不透明键。

        Args:
            image_id: 兰空图床图片键。

        Returns:
            服务器确认后返回 True。
        """
        if not re.fullmatch(r"[A-Za-z0-9]+", image_id):
            raise ValueError("Invalid Lsky image key")
        self._request("DELETE", f"images/{quote(image_id, safe='')}")
        self._inventory = None
        return True

    def download_image(self, image_info: ImageInfo, save_path: Path) -> bool:
        """发布前校验原始字节，不发送 API 凭据。

        Args:
            image_info: 受管远端元数据。
            save_path: 本地目标路径。

        Returns:
            校验和验证与原子替换完成后返回 True。
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
        """释放 API 连接池。"""
        self.session.close()
