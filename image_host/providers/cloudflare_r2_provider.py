"""Cloudflare R2 存储，使用跨平台对象路径与受限的 S3 请求。"""

import mimetypes
import os
from pathlib import Path
from urllib.parse import quote

import certifi
import urllib3
from minio import Minio

from ..core.file_handler import (
    SUPPORTED_FORMATS,
    file_fingerprint,
    normalize_relative_path,
    save_image_stream,
)
from ..interfaces.image_host import ImageHostInterface, ImageInfo


class CloudflareR2Error(Exception):
    """R2 适配器错误。"""


class CloudflareR2Provider(ImageHostInterface):
    """完整保留相对于所选本地图片库的路径。"""

    def __init__(self, config: dict):
        required = {"account_id", "access_key_id", "secret_access_key", "bucket_name"}
        missing = sorted(
            field for field in required if not str(config.get(field) or "").strip()
        )
        if missing:
            raise ValueError(f"Missing R2 configuration: {', '.join(missing)}")
        self.config = dict(config)
        self.account_id = config["account_id"]
        self.bucket_name = config["bucket_name"]
        self.local_dir = Path(config["local_dir"]).resolve()
        self.prefix = normalize_relative_path(
            str(config.get("prefix") or "memes").strip("/")
        )
        self.public_url = str(config.get("public_url") or "").rstrip("/")
        self._http = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=10, read=60),
            cert_reqs="CERT_REQUIRED",
            ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where(),
            retries=urllib3.Retry(
                total=2,
                redirect=0,
                backoff_factor=0.2,
                status_forcelist=[429, 500, 502, 503, 504],
                respect_retry_after_header=False,
            ),
        )
        self.s3_client = Minio(
            f"{self.account_id}.r2.cloudflarestorage.com",
            access_key=config["access_key_id"],
            secret_key=config["secret_access_key"],
            secure=True,
            region="auto",
            http_client=self._http,
        )
        self.s3_client.disable_virtual_style_endpoint()

    def upload_image(self, file_path: Path) -> ImageInfo:
        """将图片流式上传到 R2，并附带内容指纹。

        Args:
            file_path: 已配置根目录下的本地图片。

        Returns:
            已确认的对象元数据，含 ETag 与 SHA-256。
        """
        relative = normalize_relative_path(
            file_path.resolve().relative_to(self.local_dir).as_posix()
        )
        key = f"{self.prefix}/{relative}"
        fingerprint = file_fingerprint(file_path)
        with file_path.open("rb") as stream:
            response = self.s3_client.put_object(
                self.bucket_name,
                key,
                stream,
                fingerprint["size"],
                content_type=mimetypes.guess_type(file_path.name)[0]
                or "application/octet-stream",
                metadata={"sha256": fingerprint["sha256"]},
                num_parallel_uploads=1,
            )
        category, _, filename = relative.rpartition("/")
        return {
            "id": key,
            "relative_path": relative,
            "filename": filename,
            "category": category,
            "url": self._get_public_url(key),
            "size": fingerprint["size"],
            "sha256": fingerprint["sha256"],
            # 保留已有同步基线所使用的带引号传输格式。
            "etag": f'"{response.etag}"' if response.etag else "",
        }

    def delete_image(self, image_id: str) -> bool:
        """删除已配置前缀内经过校验的对象 ID。

        Args:
            image_id: 此适配器返回的完整 R2 对象键。

        Returns:
            S3 删除请求是否完成。
        """
        self._parse_s3_key(image_id)
        self.s3_client.remove_object(self.bucket_name, image_id)
        return True

    def get_image_list(self) -> list[ImageInfo]:
        """列出所有分页；发生失败时向上传播，不返回部分结果。

        Returns:
            所选前缀下的完整图片元数据。
        """
        images = []
        for obj in self.s3_client.list_objects(
            self.bucket_name, prefix=f"{self.prefix}/", recursive=True
        ):
            key = obj.object_name
            if not key:
                raise CloudflareR2Error("R2 inventory contains an unnamed object")
            if obj.is_dir or Path(key).suffix.lower() not in SUPPORTED_FORMATS:
                continue
            category, filename = self._parse_s3_key(key)
            relative = f"{category}/{filename}" if category else filename
            images.append(
                {
                    "id": key,
                    "relative_path": relative,
                    "filename": filename,
                    "category": category,
                    "url": self._get_public_url(key),
                    "size": obj.size,
                    "etag": f'"{obj.etag}"' if obj.etag else "",
                }
            )
        return images

    def download_image(self, image_info: ImageInfo, save_path: Path) -> bool:
        """下载指定对象版本，并在替换前校验。

        Args:
            image_info: R2 对象元数据。
            save_path: 本地目标路径。

        Returns:
            完整图片发布后返回 True。
        """
        key = image_info["id"]
        self._parse_s3_key(key)
        headers = {"If-Match": image_info["etag"]} if image_info.get("etag") else {}
        response = self.s3_client.get_object(
            self.bucket_name, key, request_headers=headers
        )
        try:
            content_length = response.headers.get("Content-Length")
            save_image_stream(
                response.stream(amt=1024 * 1024),
                Path(save_path),
                int(content_length) if content_length is not None else None,
                response.headers.get("x-amz-meta-sha256", ""),
            )
        finally:
            response.close()
            response.release_conn()
        return True

    def _generate_s3_key(self, file_path: Path) -> str:
        """构造对象键并保留嵌套分类。

        Args:
            file_path: 本地根目录内的图片。

        Returns:
            已配置前缀内的对象键。
        """
        relative = normalize_relative_path(
            file_path.resolve().relative_to(self.local_dir).as_posix()
        )
        return f"{self.prefix}/{relative}"

    def _get_category_from_path(self, file_path: Path) -> str:
        """解析相对于所选图片库的完整分类。

        Args:
            file_path: 本地图片。

        Returns:
            相对分类；根目录图片返回空字符串。
        """
        relative = normalize_relative_path(
            file_path.resolve().relative_to(self.local_dir).as_posix()
        )
        return relative.rpartition("/")[0]

    def _parse_s3_key(self, s3_key: str) -> tuple[str, str]:
        """检查对象命名空间边界，并解码跨平台路径。

        Args:
            s3_key: 完整对象键。

        Returns:
            分类与文件名。

        Raises:
            ValueError: 对象键不在已配置命名空间内。
        """
        prefix = f"{self.prefix}/"
        if not s3_key.startswith(prefix):
            raise ValueError("R2 object is outside the configured prefix")
        relative = normalize_relative_path(s3_key[len(prefix) :])
        category, _, filename = relative.rpartition("/")
        return category, filename

    def _get_public_url(self, s3_key: str) -> str:
        """仅在配置了实际公开端点时构造编码后的 URL。

        Args:
            s3_key: 完整 R2 对象键。

        Returns:
            公开 URL；私有存储桶返回空字符串。
        """
        return f"{self.public_url}/{quote(s3_key, safe='/')}" if self.public_url else ""

    def close(self) -> None:
        """释放 S3 连接池。"""
        self._http.clear()
