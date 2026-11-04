"""存储适配器提供完整清单与不透明的远端标识符。"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypedDict


class ImageInfo(TypedDict, total=False):
    """跨平台图片元数据；ETag 是版本令牌，不假定其为哈希。"""

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
    """将提供方专用路径与 HTTP 行为隔离在同步引擎之外。

    提供方方法会执行阻塞网络输入输出。应在同步工作进程中运行，
    或在异步处理器中使用 asyncio.to_thread 执行整个操作，
    清单与状态查询也同样处理。
    """

    @abstractmethod
    def upload_image(self, file_path: Path) -> ImageInfo:
        """上传图片，仅在确认成功后返回元数据。

        Args:
            file_path: 已配置本地目录内的图片。

        Returns:
            包含不透明 ID 与跨平台相对路径的元数据。
        """

    @abstractmethod
    def delete_image(self, image_id: str) -> bool:
        """删除此适配器返回的确切不透明 ID。

        Args:
            image_id: 远端标识符，与相对路径不同。

        Returns:
            对象是否已删除或原本就不存在。
        """

    @abstractmethod
    def get_image_list(self) -> list[ImageInfo]:
        """列出已配置远端命名空间中的所有图片。

        Returns:
            完整清单，包含可获取的大小与版本令牌。

        Raises:
            Exception: 任一页或目录无法列出。返回
                不完整清单可能导致镜像操作误删数据。
        """

    @abstractmethod
    def download_image(self, image_info: ImageInfo, save_path: Path) -> bool:
        """下载并校验图片后，再原子替换目标文件。

        Args:
            image_info: 此适配器返回的元数据。
            save_path: 同步引擎选择的目标路径。

        Returns:
            是否成功保存了完整图片。
        """

    def close(self) -> None:
        """释放此适配器拥有的连接。"""
