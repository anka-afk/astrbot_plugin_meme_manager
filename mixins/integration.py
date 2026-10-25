"""Public pack-management interfaces for other plugins."""

import asyncio

from ..backend.packs import storage as pack_storage
from ..backend.packs.protocol import validate_pack_id


class IntegrationMixin:
    """Expose pack operations through the plugin instance."""

    async def get_pack_status(self, pack_id: str) -> dict:
        """Read local pack availability without network access.

        Args:
            pack_id: Pack identity to inspect.

        Returns:
            Pack ID, local image count, and missing, ready, or error status.

        Raises:
            ValueError: The pack ID is invalid.
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
        """Install a caller-selected source while preserving existing selections.

        Args:
            source: GitHub source descriptor with repo, ref, and subpath.
            expected_pack_id: Optional required manifest ID; collisions fail.

        Returns:
            The storage install result, including the actual pack ID.

        Raises:
            ValueError: Source, archive, or expected identity is invalid.
            FileExistsError: The required pack ID already exists.
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
