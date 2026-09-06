"""Cloudflare R2 storage with portable object paths and bounded S3 requests."""

import mimetypes
from pathlib import Path
from urllib.parse import quote

import boto3
from botocore.config import Config

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
        self.s3_client = boto3.client(
            "s3",
            endpoint_url=f"https://{self.account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=10,
                read_timeout=60,
                retries={"mode": "standard", "total_max_attempts": 3},
            ),
        )

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
                Bucket=self.bucket_name,
                Key=key,
                Body=stream,
                ContentLength=fingerprint["size"],
                ContentType=mimetypes.guess_type(file_path.name)[0]
                or "application/octet-stream",
                Metadata={"sha256": fingerprint["sha256"]},
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
            "etag": response.get("ETag", ""),
        }

    def delete_image(self, image_id: str) -> bool:
        """Delete a validated object ID inside the configured prefix.

        Args:
            image_id: Full R2 object key returned by this adapter.

        Returns:
            Whether the S3 delete request completed.
        """
        self._parse_s3_key(image_id)
        self.s3_client.delete_object(Bucket=self.bucket_name, Key=image_id)
        return True

    def get_image_list(self) -> list[ImageInfo]:
        """List all pages; propagate failures instead of returning a partial result.

        Returns:
            Complete image metadata from the selected prefix.
        """
        images = []
        paginator = self.s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket_name, Prefix=f"{self.prefix}/"
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if (
                    key.endswith("/")
                    or Path(key).suffix.lower() not in SUPPORTED_FORMATS
                ):
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
                        "size": int(obj["Size"]) if "Size" in obj else None,
                        "etag": obj.get("ETag", ""),
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
        params = {"Bucket": self.bucket_name, "Key": key}
        if image_info.get("etag"):
            params["IfMatch"] = image_info["etag"]
        response = self.s3_client.get_object(**params)
        body = response["Body"]
        try:
            save_image_stream(
                body.iter_chunks(chunk_size=1024 * 1024),
                Path(save_path),
                response.get("ContentLength"),
                response.get("Metadata", {}).get("sha256", ""),
            )
        finally:
            body.close()
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
        self.s3_client.close()
