import tempfile
import unittest
from pathlib import Path

from image_host.core.sync_manager import SyncManager


class FakeImageHost:
    def __init__(self):
        self.deleted_ids = []

    def upload_image(self, file_path):
        raise OSError(f"无法上传 {file_path.name}")

    def download_image(self, image_info, save_path):
        return False

    def delete_image(self, image_id):
        self.deleted_ids.append(image_id)
        return True


class SyncFailureReportingTests(unittest.TestCase):
    def test_upload_failure_makes_sync_fail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "failed.png"
            image_path.write_bytes(b"image")
            manager = SyncManager(FakeImageHost(), Path(temp_dir))
            manager.check_sync_status = lambda: {
                "is_synced": False,
                "to_upload": [
                    {
                        "path": str(image_path),
                        "filename": image_path.name,
                        "category": "",
                    }
                ],
            }

            self.assertFalse(manager.sync_to_remote())

    def test_download_failure_makes_sync_fail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = SyncManager(FakeImageHost(), Path(temp_dir))
            manager.check_sync_status = lambda: {
                "is_synced": False,
                "to_download": [
                    {
                        "id": "happy/failed.png",
                        "filename": "failed.png",
                        "category": "happy",
                    }
                ],
            }

            self.assertFalse(manager.sync_from_remote())

    def test_overwrite_to_remote_does_not_delete_after_upload_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_host = FakeImageHost()
            manager = SyncManager(image_host, Path(temp_dir))
            manager.check_sync_status = lambda: {
                "to_delete_remote": [
                    {"id": "remote.png", "filename": "remote.png"}
                ]
            }
            manager.sync_to_remote = lambda: False

            self.assertFalse(manager.overwrite_to_remote())
            self.assertEqual(image_host.deleted_ids, [])

    def test_overwrite_from_remote_does_not_delete_after_download_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "local.png"
            local_path.write_bytes(b"image")
            manager = SyncManager(FakeImageHost(), Path(temp_dir))
            manager.check_sync_status = lambda: {
                "to_delete_local": [
                    {
                        "path": str(local_path),
                        "filename": local_path.name,
                        "category": "",
                    }
                ]
            }
            manager.sync_from_remote = lambda: False

            self.assertFalse(manager.overwrite_from_remote())
            self.assertTrue(local_path.exists())


if __name__ == "__main__":
    unittest.main()
