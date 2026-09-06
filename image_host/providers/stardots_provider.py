"""StarDots adapter preserving opaque filenames and complete pagination."""

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
    """StarDots adapter error."""


class AuthenticationError(StarDotsError):
    """StarDots rejected request authentication."""


class NetworkError(StarDotsError):
    """A StarDots request failed."""


class RateLimitError(StarDotsError):
    """StarDots rate limit was reached."""


class InvalidResponseError(StarDotsError):
    """StarDots returned incomplete or unexpected metadata."""


class StarDotsProvider(ImageHostInterface):
    """Encode categories at the API boundary and retain the exact remote ID."""

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
        """Sign a fresh request without a timezone-dependent timestamp offset.

        Returns:
            StarDots authentication headers.
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
        """Retry transient failures with fresh signatures and bounded backoff.

        Args:
            method: HTTP method.
            url: API URL.
            **kwargs: JSON, multipart body or query parameters.

        Returns:
            A validated successful API response.

        Raises:
            StarDotsError: Authentication, rate limiting or transport failure.
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
        """Encode a validated category into a StarDots filename.

        Args:
            category: Relative category.

        Returns:
            Encoded category, or an empty string for root images.
        """
        return (
            normalize_relative_path(category).replace("/", "@@DIR@@")
            if category
            else ""
        )

    def _decode_category(self, encoded: str) -> str:
        """Decode a category without remapping root images to a default category.

        Args:
            encoded: StarDots category prefix.

        Returns:
            Relative category.
        """
        return (
            normalize_relative_path(encoded.replace("@@DIR@@", "/")) if encoded else ""
        )

    @staticmethod
    def _extract_image_size(image_info: dict) -> int | None:
        """Read StarDots' byteSize field; its size field may contain formatted text.

        Args:
            image_info: API file metadata.

        Returns:
            Exact byte count when available.
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
        """Recognize API rate-limit responses.

        Args:
            message: API error text.

        Returns:
            Whether it describes a rate limit.
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
        """Upload an image using the same category encoding used by listings.

        Args:
            file_path: Image within the local root.

        Returns:
            Confirmed image metadata with the exact server filename.
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
        # The API limit bounds the multipart buffer; bytes also make retries replayable.
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
        # Public links stay usable; private access tickets must be requested afresh.
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
        """Delete the server filename, keeping category separators intact.

        Args:
            image_id: Opaque filename returned by StarDots.

        Returns:
            True after the API confirms deletion.
        """
        normalize_relative_path(image_id)
        self._make_request(
            "DELETE",
            f"{self.base_url}/openapi/file/delete",
            json={"space": self.space, "filenameList": [image_id]},
        )
        return True

    def get_image_list(self) -> list[ImageInfo]:
        """Fetch every page and fail closed if pagination changes or is interrupted.

        Returns:
            Complete image metadata with opaque IDs and portable relative paths.

        Raises:
            StarDotsError: Any page fails or the inventory changes during traversal.
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
        """Request a fresh ticket and atomically save the validated response.

        Args:
            image_info: StarDots image metadata.
            save_path: Local destination.

        Returns:
            True once the image has been validated and saved.
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
        """Close the HTTP connection pool."""
        self.session.close()
