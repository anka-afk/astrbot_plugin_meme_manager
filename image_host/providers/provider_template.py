from pathlib import Path

from ..interfaces.image_host import ImageHostInterface, ImageInfo


class ProviderTemplate(ImageHostInterface):
    """Template for adapters implementing complete inventories and atomic downloads."""

    def __init__(self, config: dict):
        self.config = config

    def upload_image(self, file_path: Path) -> ImageInfo:
        """Upload a file using its path relative to config['local_dir'].

        Args:
            file_path: Local image within the configured root.

        Returns:
            Opaque id, relative_path, filename, category and optional size,
            sha256, etag, modified and public url fields.
        """
        raise NotImplementedError

    def delete_image(self, image_id: str) -> bool:
        """Delete the opaque ID returned by this adapter.

        Args:
            image_id: Provider-native object identifier.

        Returns:
            Whether the object is now absent.
        """
        raise NotImplementedError

    def get_image_list(self) -> list[ImageInfo]:
        """List every image in the configured namespace.

        Returns:
            A complete list using the same relative paths as upload_image.

        Raises:
            Exception: Any page or directory could not be listed completely.
        """
        raise NotImplementedError

    def download_image(self, image_info: ImageInfo, save_path: Path) -> bool:
        """Use save_image_stream to validate and atomically publish a download.

        Args:
            image_info: Metadata returned by get_image_list.
            save_path: Local destination.

        Returns:
            Whether the complete image was saved.
        """
        raise NotImplementedError
