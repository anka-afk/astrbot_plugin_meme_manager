"""Storage adapters expose complete listings and opaque remote identifiers."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypedDict


class ImageInfo(TypedDict, total=False):
    """Portable image metadata; ETags are version tokens, never assumed hashes."""

    id: str
    relative_path: str
    filename: str
    category: str
    url: str
    size: int | None
    sha256: str
    etag: str
    modified: str


class ImageHostInterface(ABC):
    """Keep provider-specific paths and HTTP behavior outside the sync engine."""

    @abstractmethod
    def upload_image(self, file_path: Path) -> ImageInfo:
        """Upload an image, returning metadata only after confirmed success.

        Args:
            file_path: Image inside the configured local directory.

        Returns:
            Metadata including its opaque ID and portable relative path.
        """

    @abstractmethod
    def delete_image(self, image_id: str) -> bool:
        """Delete the exact opaque ID returned by this adapter.

        Args:
            image_id: Remote identifier, distinct from the relative path.

        Returns:
            Whether the object was deleted or already absent.
        """

    @abstractmethod
    def get_image_list(self) -> list[ImageInfo]:
        """List every image in the configured remote namespace.

        Returns:
            A complete listing, including sizes and version tokens when available.

        Raises:
            Exception: Any page or directory could not be listed. Returning a
                partial listing could cause mirror operations to delete data.
        """

    @abstractmethod
    def download_image(self, image_info: ImageInfo, save_path: Path) -> bool:
        """Download and validate an image before atomically replacing its target.

        Args:
            image_info: Metadata returned by this adapter.
            save_path: Destination chosen by the sync engine.

        Returns:
            Whether the complete image was saved successfully.
        """

    def close(self) -> None:
        """Release connections owned by this adapter."""
