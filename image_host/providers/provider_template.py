from pathlib import Path

from ..interfaces.image_host import ImageHostInterface, ImageInfo


class ProviderTemplate(ImageHostInterface):
    """适配器模板，实现完整清单与原子下载。"""

    def __init__(self, config: dict):
        self.config = config

    def upload_image(self, file_path: Path) -> ImageInfo:
        """使用相对于 config['local_dir'] 的路径上传文件。

        Args:
            file_path: 已配置根目录内的本地图片。

        Returns:
            不透明 id、relative_path、filename、category 以及可选的 size、
            sha256、etag、modified 和公开 url 字段。
        """
        raise NotImplementedError

    def delete_image(self, image_id: str) -> bool:
        """删除此适配器返回的不透明 ID。

        Args:
            image_id: 提供方原生对象标识符。

        Returns:
            对象当前是否已不存在。
        """
        raise NotImplementedError

    def get_image_list(self) -> list[ImageInfo]:
        """列出已配置命名空间内的所有图片。

        Returns:
            完整清单，使用与 upload_image 相同的相对路径。

        Raises:
            Exception: 任一页或目录无法完整列出。
        """
        raise NotImplementedError

    def download_image(self, image_info: ImageInfo, save_path: Path) -> bool:
        """使用 save_image_stream 校验下载内容并原子发布。

        Args:
            image_info: get_image_list 返回的元数据。
            save_path: 本地目标路径。

        Returns:
            是否保存了完整图片。
        """
        raise NotImplementedError
