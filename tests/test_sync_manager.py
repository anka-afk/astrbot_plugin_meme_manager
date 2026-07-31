import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_host.core.sync_manager import SyncManager
from image_host.providers import stardots_provider


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


class StarDotsFilenameRegressionTests(unittest.TestCase):
    def test_download_uses_the_same_category_encoding_as_upload(self):
        class TicketResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"success": True, "data": {"ticket": "ticket-value"}}

        class DownloadResponse:
            status_code = 200
            headers = {"Content-Type": "image/png", "Content-Length": "1001"}
            text = ""

            @staticmethod
            def iter_content(chunk_size):
                yield b"x" * 1001

        cases = (
            ("", "meme.png"),
            ("default", "default@@CAT@@meme.png"),
            ("animals/cats", "animals@@DIR@@cats@@CAT@@meme.png"),
        )
        for category, expected_remote_name in cases:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as temp_dir:
                provider = object.__new__(stardots_provider.StarDotsProvider)
                provider.space = "test-space"
                provider.base_url = "https://api.stardots.io"
                provider._sync_server_time = lambda: None
                provider._generate_headers = lambda: {}
                requested_filenames = []

                def make_request(method, url, **kwargs):
                    requested_filenames.append(kwargs["json"]["filename"])
                    return TicketResponse()

                provider._make_request = make_request
                save_path = Path(temp_dir) / "meme.png"

                with patch.object(
                    stardots_provider.requests,
                    "get",
                    return_value=DownloadResponse(),
                ):
                    downloaded = provider.download_image(
                        {"category": category, "filename": "meme.png"}, save_path
                    )

                self.assertTrue(downloaded)
                self.assertEqual(requested_filenames, [expected_remote_name])
                self.assertEqual(save_path.stat().st_size, 1001)


if __name__ == "__main__":
    unittest.main()
