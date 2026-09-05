import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from image_host.core.file_handler import FileHandler
from image_host.core.sync_manager import SyncManager
from image_host.core.upload_tracker import UploadTracker
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
                "to_delete_remote": [{"id": "remote.png", "filename": "remote.png"}]
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
            with (
                self.subTest(category=category),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
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


class SyncStatusTests(unittest.TestCase):
    def test_file_handler_scans_supported_images_with_relative_categories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "animals" / "cats").mkdir(parents=True)
            (root / "animals" / "cats" / "meme.PNG").write_bytes(b"image")
            (root / "ignore.txt").write_bytes(b"ignore")
            images = FileHandler(root).scan_local_images()
            self.assertEqual(
                images,
                [
                    {
                        "path": str(root / "animals" / "cats" / "meme.PNG"),
                        "id": "animals/cats/meme.PNG",
                        "filename": "meme.PNG",
                        "category": "animals/cats",
                    }
                ],
            )

    def test_remote_id_normalization_supports_all_providers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = SyncManager(FakeImageHost(), Path(temp_dir))
            self.assertEqual(
                manager._normalize_remote_id("/memes/happy/meme.png", "cloudflare_r2"),
                "happy/meme.png",
            )
            manager.image_host.config = {"base_path": "remote/memes"}
            self.assertEqual(
                manager._normalize_remote_id("remote/memes/happy/meme.png", "webdav"),
                "happy/meme.png",
            )
            self.assertEqual(
                manager._normalize_remote_id(
                    "animals@@DIR@@cats@@CAT@@meme.png", "stardots"
                ),
                "animals/cats/meme.png",
            )
            self.assertEqual(
                manager._normalize_remote_id("@@CAT@@meme.png", "unknown"),
                "meme.png",
            )

    def test_sync_status_classifies_local_and_remote_differences(self):
        class ListedImageHost(FakeImageHost):
            config = {"provider": "cloudflare_r2"}

            @staticmethod
            def get_image_list():
                return [
                    {
                        "id": "memes/common.png",
                        "filename": "common.png",
                        "category": "",
                        "size": "6",
                    },
                    {
                        "id": "memes/remote.png",
                        "filename": "remote.png",
                        "category": "",
                        "fileSize": 10,
                    },
                ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "common.png").write_bytes(b"common")
            (root / "local.png").write_bytes(b"only")
            manager = SyncManager(ListedImageHost(), root)
            status = manager.check_sync_status()
            self.assertEqual(
                [item["filename"] for item in status["to_upload"]], ["local.png"]
            )
            self.assertEqual(
                [item["filename"] for item in status["to_download"]], ["remote.png"]
            )
            self.assertEqual(status["to_delete_remote"], status["to_download"])
            self.assertEqual(
                [item["filename"] for item in status["to_delete_local"]],
                ["local.png"],
            )
            self.assertFalse(status["is_synced"])
            self.assertEqual(status["remote_image_count"], 2)
            self.assertEqual(status["remote_total_bytes"], 16)
            self.assertEqual(status["local_total_bytes"], 10)

    def test_sync_status_uses_tracker_and_estimates_missing_remote_sizes(self):
        class ListedImageHost(FakeImageHost):
            config = {"provider": "webdav"}

            @staticmethod
            def get_image_list():
                return [
                    {
                        "id": "common.png",
                        "filename": "common.png",
                        "category": "",
                    }
                ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "common.png"
            image_path.write_bytes(b"common")
            tracker = UploadTracker(root / ".tracker.json")
            manager = SyncManager(ListedImageHost(), root, tracker)
            status = manager.check_sync_status()
            self.assertEqual(status["to_upload"][0]["filename"], "common.png")
            self.assertEqual(status["remote_size_source"], "local_estimate")
            self.assertEqual(status["remote_total_bytes_estimated"], 6)

    def test_sync_success_uploads_downloads_and_updates_tracker(self):
        class SuccessfulImageHost(FakeImageHost):
            def __init__(self):
                super().__init__()
                self.uploaded = []

            def upload_image(self, file_path):
                self.uploaded.append(file_path.name)
                return {"url": f"https://example/{file_path.name}"}

            def download_image(self, image_info, save_path):
                save_path.write_bytes(b"downloaded")
                return True

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_path = root / "upload.png"
            upload_path.write_bytes(b"upload")
            tracker = UploadTracker(root / ".tracker.json")
            image_host = SuccessfulImageHost()
            manager = SyncManager(image_host, root, tracker)
            manager.check_sync_status = lambda: {
                "is_synced": False,
                "to_upload": [
                    {
                        "path": str(upload_path),
                        "filename": upload_path.name,
                        "category": "",
                    }
                ],
            }
            self.assertTrue(manager.sync_to_remote())
            self.assertEqual(image_host.uploaded, ["upload.png"])
            self.assertTrue(tracker.is_uploaded(upload_path))

            manager.check_sync_status = lambda: {
                "is_synced": False,
                "to_download": [
                    {
                        "id": "happy/download.png",
                        "filename": "download.png",
                        "category": "happy",
                    }
                ],
            }
            self.assertTrue(manager.sync_from_remote())
            self.assertEqual(
                (root / "happy" / "download.png").read_bytes(), b"downloaded"
            )

    def test_overwrite_reports_delete_failures(self):
        class FailedDeleteHost(FakeImageHost):
            def delete_image(self, image_id):
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = SyncManager(FailedDeleteHost(), Path(temp_dir))
            manager.check_sync_status = lambda: {
                "to_delete_remote": [{"id": "remote.png", "filename": "remote.png"}]
            }
            manager.sync_to_remote = lambda: True
            self.assertFalse(manager.overwrite_to_remote())


@pytest.mark.parametrize(
    ("image_info", "expected"),
    [
        ({"size": 12}, 12),
        ({"file_size": 3.8}, 3),
        ({"bytes": "9"}, 9),
        ({"length": "bad"}, None),
        ({}, None),
    ],
)
def test_extract_remote_size(image_info, expected):
    manager = object.__new__(SyncManager)
    assert manager._extract_remote_size(image_info) == expected


if __name__ == "__main__":
    unittest.main()
