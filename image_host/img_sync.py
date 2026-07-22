import asyncio
import logging
import multiprocessing
import sys
from pathlib import Path

from .core.sync_manager import SyncManager
from .core.upload_tracker import UploadTracker
from .providers import CloudflareR2Provider, StarDotsProvider, WebDAVProvider

logger = logging.getLogger(__name__)


class ImageSync:
    """图片同步客户端

    用于在本地目录和远程图床之间同步图片文件。支持目录结构，
    可以保持本地目录分类在远程图床中。

    基本用法:
        sync = ImageSync(config={
            "key": "your_key",
            "secret": "your_secret",
            "space": "your_space"
        }, local_dir="path/to/images")

        # 检查同步状态
        status = sync.check_status()

        # 上传本地新文件到远程
        sync.upload_to_remote()

        # 下载远程新文件到本地
        sync.download_to_local()

        # 完全同步（双向）
        sync.sync_all()
    """

    def __init__(
        self,
        config: dict[str, str],
        local_dir: str | Path,
        provider_type: str = "stardots",
    ):
        """
        初始化同步客户端

        Args:
            config: 包含图床配置信息的字典
            local_dir: 本地图片目录的路径
            provider_type: 图床提供者类型，可选 "stardots"、"cloudflare_r2" 或 "webdav"
        """
        self.config = config
        self.local_dir = Path(local_dir)
        self.provider_type = provider_type

        # 根据 provider_type 初始化对应的 provider
        if provider_type == "stardots":
            self.provider = StarDotsProvider(
                {
                    "key": config["key"],
                    "secret": config["secret"],
                    "space": config["space"],
                    "local_dir": str(local_dir),
                }
            )
        elif provider_type == "cloudflare_r2":
            self.provider = CloudflareR2Provider(config)
        elif provider_type == "webdav":
            self.provider = WebDAVProvider({**config, "local_dir": str(local_dir)})
        else:
            raise ValueError(f"不支持的图床提供者类型: {provider_type}")

        # 初始化上传追踪器（仅用于记录已上传文件）
        tracker_file = Path(local_dir) / ".upload_tracker.json"
        self.upload_tracker = UploadTracker(tracker_file)

        self.sync_manager = SyncManager(
            image_host=self.provider,
            local_dir=self.local_dir,
            upload_tracker=self.upload_tracker,
        )

        self.sync_process = None
        self._sync_task = None

    def check_status(self) -> dict[str, list[dict[str, str]]]:
        """
        检查同步状态

        Returns:
            包含需要上传和下载的文件信息的字典:
            {
                "to_upload": [{"filename": "1.jpg", "category": "cats"}],
                "to_download": [{"filename": "2.jpg", "category": "dogs"}]
            }
        """
        return self.sync_manager.check_sync_status()

    async def start_sync(self, task: str) -> bool:
        """
        启动同步任务并异步等待完成

        Args:
            task: 同步任务类型 ('upload', 'download', 'sync_all')

        Returns:
            同步是否成功
        """
        # 不能为了启动新任务而悄悄杀掉已有任务。
        if self.sync_process and self.sync_process.is_alive():
            raise RuntimeError("已有同步任务正在运行，请等待完成后再试")

        # 检查是否需要同步
        status = self.check_status()
        if task == "upload" and not status.get("to_upload"):
            logger.info("没有文件需要上传")
            return True
        elif task == "download" and not status.get("to_download"):
            logger.info("没有文件需要下载")
            return True
        elif task == "overwrite_to_remote" and not (
            status.get("to_upload") or status.get("to_delete_remote")
        ):
            logger.info("云端已是最新且完全一致，无需覆盖")
            return True
        elif task == "overwrite_from_remote" and not (
            status.get("to_download") or status.get("to_delete_local")
        ):
            logger.info("本地已是最新且完全一致，无需覆盖")
            return True

        # 创建并启动进程
        self.sync_process = multiprocessing.Process(
            target=run_sync_process, args=(self.config, str(self.local_dir), task)
        )
        self.sync_process.start()

        # 创建异步任务来等待进程完成
        loop = asyncio.get_event_loop()
        self._sync_task = loop.run_in_executor(None, self.sync_process.join)

        try:
            # 等待进程完成
            await self._sync_task
            exit_code = self.sync_process.exitcode
            if exit_code == 0:
                logger.info("同步任务完成成功")
                return True
            else:
                logger.error(f"同步任务失败，进程退出码: {exit_code}")
                return False
        except Exception as e:
            logger.error(f"同步任务异常: {str(e)}")
            self.stop_sync()
            return False

    def stop_sync(self):
        """停止当前正在运行的同步任务"""
        if self.sync_process and self.sync_process.is_alive():
            self.sync_process.terminate()
            self.sync_process.join(timeout=5)
            if self.sync_process.is_alive():
                self.sync_process.kill()
            self.sync_process = None
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            self._sync_task = None

    def upload_to_remote(self) -> multiprocessing.Process:
        """
        在独立进程中将本地新文件上传到远程

        Returns:
            同步进程对象
        """
        # 总是返回进程对象，让进程内部处理是否需要同步
        self.sync_process = self._start_sync_process("upload")
        return self.sync_process

    def download_to_local(self) -> multiprocessing.Process:
        """
        在独立进程中将远程新文件下载到本地

        Returns:
            同步进程对象
        """
        # 总是返回进程对象，让进程内部处理是否需要同步
        self.sync_process = self._start_sync_process("download")
        return self.sync_process

    def sync_all(self) -> bool:
        """
        执行完整的双向同步

        先上传本地新文件，再下载远程新文件

        Returns:
            同步是否成功
        """
        upload_success = self.upload_to_remote()
        download_success = self.download_to_local()
        return upload_success and download_success

    def get_remote_files(self) -> list[dict[str, str]]:
        """
        获取远程文件列表

        Returns:
            远程文件信息列表:
            [
                {
                    "filename": "1.jpg",
                    "category": "cats",
                    "url": "https://..."
                }
            ]
        """
        return self.provider.get_image_list()

    def delete_remote_file(self, filename: str) -> bool:
        """
        删除远程文件

        Args:
            filename: 要删除的文件名

        Returns:
            删除是否成功
        """
        return self.provider.delete_image(filename)

    def _start_sync_process(self, task: str) -> multiprocessing.Process:
        """
        在独立进程中运行同步任务
        """
        # 创建进程对象
        process = multiprocessing.Process(
            target=run_sync_process, args=(self.config, str(self.local_dir), task)
        )

        # 启动进程
        process.start()
        return process


def run_sync_process(config: dict[str, str], local_dir: str, task: str):
    """
    在独立进程中运行同步任务
    """
    try:
        logger.info(f"启动同步进程，任务类型: {task}, 本地目录: {local_dir}")

        # 检测配置类型并提取正确的配置
        if "cloudflare_r2" in config:
            # 如果是完整配置，提取 cloudflare_r2 部分
            provider_config = config["cloudflare_r2"]
            provider_type = "cloudflare_r2"
        elif "stardots" in config:
            # 如果是 stardots 配置
            provider_config = config["stardots"]
            provider_type = "stardots"
        elif "webdav" in config:
            # 如果是完整配置，提取 webdav 部分
            provider_config = config["webdav"]
            provider_type = "webdav"
        elif "account_id" in config:
            # 如果是直接的 R2 配置
            provider_config = config
            provider_type = "cloudflare_r2"
        elif "key" in config:
            # 如果是直接的 stardots 配置
            provider_config = config
            provider_type = "stardots"
        elif config.get("provider") == "webdav" or all(
            config.get(field) for field in ("url", "username", "password")
        ):
            # 如果是直接的 WebDAV 配置
            provider_config = config
            provider_type = "webdav"
        else:
            logger.error(f"无法识别的配置格式: {list(config.keys())}")
            sys.exit(1)

        sync = ImageSync(provider_config, local_dir, provider_type)

        if task == "upload":
            logger.info("开始上传任务")
            success = sync.sync_manager.sync_to_remote()
            logger.info(f"上传任务完成，成功: {success}")
            sys.exit(0 if success else 1)
        elif task == "download":
            logger.info("开始下载任务")
            success = sync.sync_manager.sync_from_remote()
            logger.info(f"下载任务完成，成功: {success}")
            sys.exit(0 if success else 1)
        elif task == "sync_all":
            logger.info("开始完整同步任务")
            upload_success = sync.sync_manager.sync_to_remote()
            download_success = sync.sync_manager.sync_from_remote()
            logger.info(
                f"完整同步完成，上传成功: {upload_success}, 下载成功: {download_success}"
            )
            sys.exit(0 if upload_success and download_success else 1)
        elif task == "overwrite_to_remote":
            logger.info("开始覆盖到云端任务")
            success = sync.sync_manager.overwrite_to_remote()
            logger.info(f"覆盖到云端完成，成功: {success}")
            sys.exit(0 if success else 1)
        elif task == "overwrite_from_remote":
            logger.info("开始从云端覆盖任务")
            success = sync.sync_manager.overwrite_from_remote()
            logger.info(f"从云端覆盖完成，成功: {success}")
            sys.exit(0 if success else 1)
        else:
            logger.error(f"未知的任务类型: {task}")
            sys.exit(1)
    except Exception as e:
        logger.exception(f"同步进程发生异常: {str(e)}")
        sys.exit(1)
