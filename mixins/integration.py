"""供其他插件使用的公开表情包管理接口。"""

import asyncio

from ..backend.packs import storage as pack_storage
from ..backend.packs.protocol import validate_pack_id


class IntegrationMixin:
    """通过插件实例提供表情包操作。"""

    async def get_pack_status(self, pack_id: str) -> dict:
        """读取本地表情包可用状态，不访问网络。

        Args:
            pack_id: 待检查的表情包标识。

        Returns:
            表情包 ID、本地图片数量及 missing、ready 或 error 状态。

        Raises:
            ValueError: 表情包 ID 无效。
        """
        pack_id = validate_pack_id(pack_id, "pack_id")
        try:
            detail = await asyncio.to_thread(pack_storage.get_pack_detail, pack_id)
        except FileNotFoundError:
            return {"pack_id": pack_id, "status": "missing", "image_count": 0}
        image_count = detail["total_images"]
        ready = detail["manifest"].get("id") == pack_id and image_count > 0
        return {
            "pack_id": pack_id,
            "status": "ready" if ready else "error",
            "image_count": image_count,
        }

    async def install_pack(
        self, source: dict, *, expected_pack_id: str | None = None
    ) -> dict:
        """安装调用方选择的来源，并保留现有选包规则。

        Args:
            source: 包含 repo、ref 与 subpath 的 GitHub 来源描述。
            expected_pack_id: 可选的必需清单 ID，冲突时失败。

        Returns:
            存储层安装结果，包含实际表情包 ID。

        Raises:
            ValueError: 来源、压缩包或预期标识无效。
            FileExistsError: 所要求的表情包 ID 已存在。
        """
        if expected_pack_id is not None:
            expected_pack_id = validate_pack_id(expected_pack_id, "expected_pack_id")
        return await self._run_guarded_runtime_file_operation(
            "安装表情包",
            pack_storage.install_pack_from_github_source,
            source=dict(source),
            overwrite=False,
            set_as_default=False,
            preserve_existing_selection=True,
            expected_pack_id=expected_pack_id,
            github_accelerator_url=self._get_github_accelerator_url(),
        )
