"""Cloudflare R2 storage with portable object paths and bounded S3 requests."""

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
    """R2 adapter error."""


class CloudflareR2Provider(ImageHostInterface):
    """Preserve full paths relative to the selected local image library."""

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
        """Stream an image to R2 with a content fingerprint.

        Args:
            file_path: Local image under the configured root.

        Returns:
            Confirmed object metadata, including its ETag and SHA-256.
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
            # Keep the quoted wire format used by existing sync baselines.
            "etag": f'"{response.etag}"' if response.etag else "",
        }

    def delete_image(self, image_id: str) -> bool:
        """Delete a validated object ID inside the configured prefix.

        Args:
            image_id: Full R2 object key returned by this adapter.

        Returns:
            Whether the S3 delete request completed.
        """
        self._parse_s3_key(image_id)
        self.s3_client.remove_object(self.bucket_name, image_id)
        return True

    def get_image_list(self) -> list[ImageInfo]:
        """List all pages; propagate failures instead of returning a partial result.

        Returns:
            Complete image metadata from the selected prefix.
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
        """Download a specific object version and validate before replacement.

        Args:
            image_info: R2 object metadata.
            save_path: Local destination.

        Returns:
            True once the complete image has been published.
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
        """Build an object key preserving nested categories.

        Args:
            file_path: Image inside the local root.

        Returns:
            A key inside the configured prefix.
        """
        relative = normalize_relative_path(
            file_path.resolve().relative_to(self.local_dir).as_posix()
        )
        return f"{self.prefix}/{relative}"

    def _get_category_from_path(self, file_path: Path) -> str:
        """Resolve the full category relative to the selected image library.

        Args:
            file_path: Local image.

        Returns:
            Relative category, or an empty string for root images.
        """
        relative = normalize_relative_path(
            file_path.resolve().relative_to(self.local_dir).as_posix()
        )
        return relative.rpartition("/")[0]

    def _parse_s3_key(self, s3_key: str) -> tuple[str, str]:
        """Check object namespace boundaries and decode its portable path.

        Args:
            s3_key: Full object key.

        Returns:
            Category and filename.

        Raises:
            ValueError: The key falls outside the configured namespace.
        """
        prefix = f"{self.prefix}/"
        if not s3_key.startswith(prefix):
            raise ValueError("R2 object is outside the configured prefix")
        relative = normalize_relative_path(s3_key[len(prefix) :])
        category, _, filename = relative.rpartition("/")
        return category, filename

    def _get_public_url(self, s3_key: str) -> str:
        """Build an encoded URL only when a real public endpoint is configured.

        Args:
            s3_key: Full R2 object key.

        Returns:
            Public URL, or an empty string for private buckets.
        """
        return f"{self.public_url}/{quote(s3_key, safe='/')}" if self.public_url else ""

    def close(self) -> None:
        """Release the S3 connection pool."""
        self._http.clear()
