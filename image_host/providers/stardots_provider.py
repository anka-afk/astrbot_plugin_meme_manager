"""StarDots 适配器，保留不透明文件名并完整处理分页。"""

import hashlib
import mimetypes
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import requests

from ..core.file_handler import (
    SUPPORTED_FORMATS,
    normalize_relative_path,
    save_image_stream,
)
from ..interfaces.image_host import ImageHostInterface, ImageInfo


class StarDotsError(Exception):
    """StarDots 适配器错误。"""


class AuthenticationError(StarDotsError):
    """StarDots 拒绝了请求身份验证。"""


class NetworkError(StarDotsError):
    """StarDots 请求失败。"""


class RateLimitError(StarDotsError):
    """已达到 StarDots 请求频率限制。"""


class InvalidResponseError(StarDotsError):
    """StarDots 返回了不完整或不符合预期的元数据。"""


class StarDotsProvider(ImageHostInterface):
    """在 API 边界编码分类，并保留确切的远端 ID。"""

    BASE_URL = "https://api.stardots.io"
    CATEGORY_SEPARATOR = "@@CAT@@"
    DEFAULT_CATEGORY = ""
    MIME_TYPES = {
        extension: mimetypes.types_map.get(extension, "image/jpeg")
        for extension in SUPPORTED_FORMATS
    }

    def __init__(self, config: dict):
        missing = [
            field for field in ("key", "secret", "space") if not config.get(field)
        ]
        if missing:
            raise ValueError(f"Missing StarDots configuration: {', '.join(missing)}")
        self.config = dict(config)
        self.key, self.secret, self.space = (
            config["key"],
            config["secret"],
            config["space"],
        )
        self.local_dir = Path(config["local_dir"]).resolve()
        self.base_url = self.BASE_URL
        self.server_time_offset = 0
        self.session = requests.Session()
        self.session.verify = True

    def _generate_headers(self) -> dict[str, str]:
        """为新请求签名，不添加依赖时区的时间戳偏移。

        Returns:
            StarDots 身份验证请求头。
        """
        timestamp = str(int(time.time() + self.server_time_offset))
        nonce = secrets.token_hex(10)
        signature = (
            hashlib.md5(f"{timestamp}|{self.secret}|{nonce}".encode())
            .hexdigest()
            .upper()
        )
        return {
            "x-stardots-timestamp": timestamp,
            "x-stardots-nonce": nonce,
            "x-stardots-key": self.key,
            "x-stardots-sign": signature,
        }

    def _make_request(self, method: str, url: str, **kwargs) -> dict:
        """使用新签名及有界退避重试临时失败。

        Args:
            method: HTTP 方法。
            url: API 请求地址。
            **kwargs: JSON、分段请求体或查询参数。

        Returns:
            已校验的成功 API 响应。

        Raises:
            StarDotsError: 身份验证、频率限制或传输失败。
        """
        extra_headers = kwargs.pop("headers", {})
        for attempt in range(3):
            response = None
            delay = 2**attempt
            try:
                response = self.session.request(
                    method,
                    url,
                    headers={**self._generate_headers(), **extra_headers},
                    timeout=(10, 60),
                    allow_redirects=False,
                    **kwargs,
                )
                if response.status_code in (401, 403):
                    raise AuthenticationError("StarDots authentication failed")
                if response.status_code == 429 or response.status_code in (
                    500,
                    502,
                    503,
                    504,
                ):
                    limited = response.status_code == 429
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        delay = max(delay, int(retry_after))
                    if attempt == 2 or delay > 30:
                        error = RateLimitError if limited else NetworkError
                        raise error(
                            f"StarDots request failed: HTTP {response.status_code}"
                        )
                elif response.status_code != 200:
                    raise InvalidResponseError(
                        f"StarDots request failed: HTTP {response.status_code}"
                    )
                else:
                    result = response.json()
                    if not isinstance(result, dict):
                        raise InvalidResponseError(
                            "StarDots returned an invalid response"
                        )
                    if result.get("success") is True:
                        return result
                    message = str(result.get("message") or "")
                    lowered = message.lower()
                    if "invalid timestamp" in lowered or "invalid nonce" in lowered:
                        server_ts = result.get("ts")
                        if attempt == 2 or not isinstance(server_ts, (int, float)):
                            raise AuthenticationError(
                                "StarDots timestamp or nonce was rejected"
                            )
                        self.server_time_offset = server_ts / 1000 - time.time()
                    elif self._is_rate_limit_error(message):
                        if attempt == 2:
                            raise RateLimitError("StarDots API rate limit exceeded")
                    else:
                        raise InvalidResponseError(
                            f"StarDots rejected the operation: {message[:160]}"
                        )
            except requests.exceptions.SSLError as exc:
                raise NetworkError(
                    "StarDots TLS certificate validation failed"
                ) from exc
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == 2:
                    raise NetworkError(
                        f"StarDots request failed ({type(exc).__name__})"
                    ) from exc
            except requests.RequestException as exc:
                raise NetworkError(
                    f"StarDots request failed ({type(exc).__name__})"
                ) from exc
            except ValueError as exc:
                raise InvalidResponseError("StarDots returned invalid JSON") from exc
            finally:
                if response is not None:
                    response.close()
            time.sleep(delay)
        raise NetworkError("StarDots retries exhausted")

    def _encode_category(self, category: str) -> str:
        """将已校验分类编码到 StarDots 文件名中。

        Args:
            category: 相对分类。

        Returns:
            编码后的分类；根目录图片返回空字符串。
        """
        return (
            normalize_relative_path(category).replace("/", "@@DIR@@")
            if category
            else ""
        )

    def _decode_category(self, encoded: str) -> str:
        """解码分类，不将根目录图片重新映射到默认分类。

        Args:
            encoded: StarDots 分类前缀。

        Returns:
            相对分类。
        """
        return (
            normalize_relative_path(encoded.replace("@@DIR@@", "/")) if encoded else ""
        )

    @staticmethod
    def _extract_image_size(image_info: dict) -> int | None:
        """读取 StarDots 的 byteSize 字段；size 字段可能是格式化文本。

        Args:
            image_info: API 文件元数据。

        Returns:
            确切字节数（如可获取）。
        """
        for key in ("byteSize", "fileSize", "file_size", "bytes", "length", "size"):
            value = image_info.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value >= 0
            ):
                return int(value)
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    @staticmethod
    def _is_rate_limit_error(message: str) -> bool:
        """识别 API 请求频率限制响应。

        Args:
            message: API 错误文本。

        Returns:
            是否表示请求频率限制。
        """
        lowered = str(message or "").lower()
        return any(
            word in lowered
            for word in (
                "exceed times limit",
                "rate limit",
                "too many requests",
                "请求频率",
                "调用频次",
                "调用次数",
            )
        )

    def upload_image(self, file_path: Path) -> ImageInfo:
        """使用与清单相同的分类编码上传图片。

        Args:
            file_path: 本地根目录内的图片。

        Returns:
            已确认的图片元数据，包含服务器上的确切文件名。
        """
        relative = normalize_relative_path(
            file_path.resolve().relative_to(self.local_dir).as_posix()
        )
        if "@@CAT@@" in relative or "@@DIR@@" in relative:
            raise ValueError(
                "StarDots filenames cannot contain reserved category separators"
            )
        category, _, filename = relative.rpartition("/")
        encoded = self._encode_category(category)
        remote_name = f"{encoded}@@CAT@@{filename}" if encoded else filename
        if len(remote_name) > 170 or file_path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError(
                "StarDots supports filenames up to 170 characters and files up to 10 MiB"
            )
        # 接口限制了分段上传缓冲区大小；保留字节也便于重试。
        with file_path.open("rb") as stream:
            content = stream.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("StarDots image grew beyond the upload size limit")
        result = self._make_request(
            "PUT",
            f"{self.base_url}/openapi/file/upload",
            files={
                "file": (
                    remote_name,
                    content,
                    self.MIME_TYPES.get(file_path.suffix.lower(), "image/jpeg"),
                ),
                "space": (None, self.space),
            },
        )
        data = result.get("data") or {}
        if data.get("filename") != remote_name:
            raise InvalidResponseError("StarDots changed the uploaded filename")
        # 公共链接可继续使用；私有访问凭证必须重新申请。
        url = urlsplit(str(data.get("url") or ""))
        return {
            "id": remote_name,
            "relative_path": relative,
            "filename": filename,
            "category": category,
            "url": urlunsplit((url.scheme, url.netloc, url.path, "", "")),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "modified": str(int(result["ts"] / 1000))
            if isinstance(result.get("ts"), (int, float))
            else "",
        }

    def delete_image(self, image_id: str) -> bool:
        """删除服务器文件名，完整保留分类分隔符。

        Args:
            image_id: StarDots 返回的不透明文件名。

        Returns:
            API 确认删除后返回 True。
        """
        normalize_relative_path(image_id)
        self._make_request(
            "DELETE",
            f"{self.base_url}/openapi/file/delete",
            json={"space": self.space, "filenameList": [image_id]},
        )
        return True

    def get_image_list(self) -> list[ImageInfo]:
        """读取全部分页；分页变化或中断时按失败处理。

        Returns:
            完整图片元数据，包含不透明 ID 与跨平台相对路径。

        Raises:
            StarDotsError: 任一分页失败或遍历期间清单变化。
        """
        images = []
        seen = set()
        total = None
        for page in range(1, 1001):
            result = self._make_request(
                "GET",
                f"{self.base_url}/openapi/file/list",
                params={"space": self.space, "page": page, "pageSize": 100},
            )
            data = result.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise InvalidResponseError("StarDots returned an invalid file listing")
            current_total = data.get("totalCount")
            if current_total is not None:
                if not isinstance(current_total, int) or current_total < 0:
                    raise InvalidResponseError(
                        "StarDots returned an invalid totalCount"
                    )
                if total is not None and current_total != total:
                    raise InvalidResponseError(
                        "StarDots inventory changed during pagination"
                    )
                total = current_total
            for source in data["list"]:
                remote_name = source["name"]
                if remote_name in seen:
                    raise InvalidResponseError(
                        "StarDots repeated a file during pagination"
                    )
                seen.add(remote_name)
                if "@@CAT@@" in remote_name:
                    encoded, filename = remote_name.split("@@CAT@@", 1)
                    category = self._decode_category(encoded)
                else:
                    category, filename = "", remote_name
                relative = normalize_relative_path(
                    f"{category}/{filename}" if category else filename
                )
                if "/" in filename:
                    raise InvalidResponseError(
                        "StarDots returned a filename containing a path separator"
                    )
                if Path(filename).suffix.lower() not in SUPPORTED_FORMATS:
                    continue
                url = urlsplit(str(source.get("url") or ""))
                images.append(
                    {
                        "id": remote_name,
                        "relative_path": relative,
                        "filename": filename,
                        "category": category,
                        "url": urlunsplit((url.scheme, url.netloc, url.path, "", "")),
                        "size": self._extract_image_size(source),
                        "modified": str(source["uploadedAt"])
                        if "uploadedAt" in source
                        else "",
                    }
                )
            if len(data["list"]) < 100 or (total is not None and len(seen) >= total):
                if total is not None and len(seen) != total:
                    raise InvalidResponseError(
                        "StarDots returned an incomplete file listing"
                    )
                return images
        raise InvalidResponseError(
            "StarDots pagination limit reached before listing completed"
        )

    def download_image(self, image_info: ImageInfo, save_path: Path) -> bool:
        """请求新票据，并原子保存已校验响应。

        Args:
            image_info: StarDots 图片元数据。
            save_path: 本地目标路径。

        Returns:
            图片校验并保存后返回 True。
        """
        remote_name = image_info["id"]
        normalize_relative_path(remote_name)
        result = self._make_request(
            "POST",
            f"{self.base_url}/openapi/file/ticket",
            json={"space": self.space, "filename": remote_name},
        )
        ticket = (result.get("data") or {}).get("ticket")
        if not ticket:
            raise InvalidResponseError("StarDots did not provide a download ticket")
        url = f"https://i.stardots.io/{quote(self.space, safe='')}/{quote(remote_name, safe='')}"
        response = None
        try:
            response = self.session.get(
                f"{url}?{urlencode({'ticket': ticket})}", stream=True, timeout=(10, 60)
            )
            if response.status_code != 200:
                raise NetworkError(
                    f"StarDots download failed: HTTP {response.status_code}"
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
        except requests.RequestException as exc:
            raise NetworkError(
                f"StarDots download failed: {type(exc).__name__}"
            ) from None
        finally:
            if response is not None:
                response.close()
        return True

    def close(self) -> None:
        """关闭 HTTP 连接池。"""
        self.session.close()
