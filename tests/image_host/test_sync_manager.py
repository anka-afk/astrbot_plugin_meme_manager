import hashlib
import os

import pytest
from image_host.core.file_handler import (
    FileHandler,
    file_fingerprint,
    save_image_stream,
)
from image_host.core.sync_manager import SyncManager
from image_host.core.upload_tracker import UploadTracker


@pytest.fixture
def manager(tmp_path, host):
    return SyncManager(
        host, tmp_path, UploadTracker(tmp_path / ".sync-state/tracker.json")
    )


def test_scan_preserves_nested_categories_and_hashes_content(tmp_path, make_image):
    path = make_image("animals/cats/meme.PNG")
    (tmp_path / "ignored.txt").write_text("ignored")
    images = FileHandler(tmp_path).scan_local_images()
    assert len(images) == 1
    assert images[0]["id"] == "animals/cats/meme.PNG"
    assert images[0]["category"] == "animals/cats"
    assert images[0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_same_size_edit_with_restored_timestamp_is_uploaded(
    manager, host, make_image, image_bytes
):
    path = make_image()
    assert manager.sync_to_remote()
    previous = path.stat()
    path.write_bytes(image_bytes(96))
    assert path.stat().st_size == previous.st_size
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    status = manager.check_sync_status()
    assert [image["id"] for image in status["to_upload"]] == ["happy/meme.png"]
    assert not status["conflicts"]
    assert manager.sync_to_remote()
    assert host.objects["happy/meme.png"] == path.read_bytes()
    assert manager.check_sync_status()["is_synced"]


def test_new_tracker_does_not_blindly_overwrite_existing_remote(
    manager, host, make_image, image_bytes
):
    path = make_image()
    host.objects["happy/meme.png"] = image_bytes(96)
    status = manager.check_sync_status()
    assert status["to_upload"] == []
    assert status["conflicts"] == [
        {"relative_path": "happy/meme.png", "reason": "unverified"}
    ]
    assert not manager.sync_to_remote()
    assert host.calls == []
    assert path.read_bytes() != host.objects["happy/meme.png"]


def test_remote_checksum_proves_equality_without_tracker(manager, host, make_image):
    path = make_image()
    host.objects["happy/meme.png"] = path.read_bytes()
    host.expose_checksum = True
    assert manager.check_sync_status()["is_synced"]
    assert manager.sync_to_remote()
    assert host.calls == []


def test_legacy_filename_record_is_not_content_proof(manager, host, make_image):
    path = make_image()
    host.objects["happy/meme.png"] = path.read_bytes()
    manager.upload_tracker.uploaded_files = {
        "happy/meme.png": {"file_size": path.stat().st_size}
    }
    manager.upload_tracker.save()
    assert manager.check_sync_status()["conflict_count"] == 1


def test_remote_edit_downloads_and_refreshes_baseline(
    manager, host, make_image, image_bytes
):
    path = make_image()
    assert manager.sync_to_remote()
    host.objects["happy/meme.png"] = image_bytes(96)
    assert (
        manager.check_sync_status()["to_download"][0]["relative_path"]
        == "happy/meme.png"
    )
    assert manager.sync_from_remote()
    assert path.read_bytes() == host.objects["happy/meme.png"]
    assert manager.upload_tracker.is_uploaded(path, "happy")
    assert manager.check_sync_status()["is_synced"]


def test_both_sides_changed_remain_conflicted(manager, host, make_image, image_bytes):
    path = make_image()
    assert manager.sync_to_remote()
    path.write_bytes(image_bytes(96))
    host.objects["happy/meme.png"] = image_bytes(128)
    status = manager.check_sync_status()
    assert status["conflicts"][0]["reason"] == "both_changed"
    assert not manager.run("sync_all")
    assert path.read_bytes() == image_bytes(96)
    assert host.objects["happy/meme.png"] == image_bytes(128)


@pytest.mark.parametrize("direction", ["remote", "local"])
def test_mirror_replaces_same_name_content_and_cleans_only_extras(
    manager, host, make_image, image_bytes, tmp_path, direction
):
    common = make_image()
    extra = make_image("local.png")
    host.objects = {"happy/meme.png": image_bytes(96), "remote.png": image_bytes(128)}
    local_content = common.read_bytes()
    if direction == "remote":
        assert manager.overwrite_to_remote()
        assert host.objects == {
            "happy/meme.png": local_content,
            "local.png": extra.read_bytes(),
        }
        assert ("delete", "opaque:remote.png") in host.calls
    else:
        assert manager.overwrite_from_remote()
        assert common.read_bytes() == image_bytes(96)
        assert (tmp_path / "remote.png").read_bytes() == image_bytes(128)
        assert not extra.exists()
    assert manager.check_sync_status()["is_synced"]


@pytest.mark.parametrize("direction", ["remote", "local"])
def test_failed_transfer_preserves_destination_extras(
    manager, host, make_image, image_bytes, direction
):
    path = make_image()
    host.objects["remote.png"] = image_bytes(96)
    host.fail_upload = host.fail_download = True
    operation = (
        manager.overwrite_to_remote
        if direction == "remote"
        else manager.overwrite_from_remote
    )
    assert not operation()
    assert path.exists()
    assert "remote.png" in host.objects
    assert not any(action == "delete" for action, _ in host.calls)


def test_incomplete_initial_listing_never_mutates_either_side(
    manager, host, make_image
):
    make_image()
    host.fail_list_at = 1
    assert not manager.overwrite_from_remote()
    assert host.calls == []
    assert manager.progress["phase"] == "failed"


def test_failed_verification_listing_prevents_cleanup(
    manager, host, make_image, image_bytes
):
    make_image()
    host.objects["remote.png"] = image_bytes(96)
    host.fail_list_at = 2
    assert not manager.overwrite_to_remote()
    assert "remote.png" in host.objects
    assert not any(action == "delete" for action, _ in host.calls)


def test_source_edit_during_transfer_prevents_mirror_cleanup(
    manager, host, make_image, image_bytes
):
    make_image()
    host.objects["remote.png"] = image_bytes(96)
    host.after_upload = lambda: make_image("new.png")
    assert not manager.overwrite_to_remote()
    assert "remote.png" in host.objects


def test_partial_download_never_replaces_existing_image(
    manager, host, make_image, image_bytes, tmp_path
):
    path = make_image()
    before = path.read_bytes()
    host.objects["happy/meme.png"] = image_bytes(96)
    host.fail_download = True
    assert not manager.overwrite_from_remote()
    assert path.read_bytes() == before
    assert not list(tmp_path.rglob("*.tmp"))


def test_edit_during_download_is_retained(manager, host, make_image, image_bytes):
    path = make_image()
    host.objects["happy/meme.png"] = image_bytes(96)
    host.after_download = lambda: path.write_bytes(image_bytes(128))
    assert not manager.overwrite_from_remote()
    assert path.read_bytes() == image_bytes(128)


def test_changed_upload_is_not_recorded(manager, host, make_image, image_bytes):
    path = make_image()
    host.after_upload = lambda: path.write_bytes(image_bytes(128))
    assert not manager.sync_to_remote()
    assert manager.upload_tracker.get_uploaded_count() == 0


def test_union_uses_one_inventory_and_retains_extras(
    manager, host, make_image, image_bytes, tmp_path
):
    make_image("local.png")
    host.objects["remote.png"] = image_bytes(96)
    assert manager.run("sync_all")
    assert host.list_calls == 1
    assert (tmp_path / "remote.png").exists()
    assert set(host.objects) == {"local.png", "remote.png"}


def test_cancellation_preserves_completed_work_and_reports_counts(
    manager, host, make_image
):
    make_image("first.png")
    make_image("second.png")
    manager.cancel_requested = lambda: manager.progress.get("succeeded", 0) >= 1
    assert not manager.sync_to_remote()
    assert manager.progress["phase"] == "cancelled"
    assert manager.progress["succeeded"] == 1
    assert len(host.objects) == 1


def test_progress_is_real_and_failed_files_are_identified(manager, host, make_image):
    make_image()
    host.fail_upload = True
    snapshots = []
    manager.progress_callback = lambda data: snapshots.append(dict(data))
    assert not manager.sync_to_remote()
    assert snapshots[-1]["total"] == 1
    assert snapshots[-1]["processed"] == 1
    assert snapshots[-1]["failed"] == 1
    assert snapshots[-1]["errors"][0]["path"] == "happy/meme.png"


@pytest.mark.parametrize(
    "relative",
    [
        "../escape.png",
        "C:/escape.png",
        "safe/../escape.png",
        "name.png:stream",
        "CON.png",
        "unsafe./file.png",
        ".sync-state/file.png",
        ".SYNC-STATE/file.png",
    ],
)
def test_unsafe_remote_paths_fail_before_any_transfer(
    manager, host, image_bytes, relative
):
    host.objects[relative] = image_bytes()
    assert not manager.overwrite_from_remote()
    assert host.calls == []


def test_duplicate_remote_paths_fail_closed(manager, host, image_bytes):
    host.objects["happy/meme.png"] = image_bytes()
    info = host.metadata("happy/meme.png")
    host.get_image_list = lambda: [info, {**info, "id": "other-object"}]
    with pytest.raises(ValueError, match="Duplicate"):
        manager.check_sync_status()


def test_legitimate_memes_category_is_not_stripped(manager, host, make_image):
    path = make_image("memes/happy/meme.png")
    host.objects["memes/happy/meme.png"] = path.read_bytes()
    host.expose_checksum = True
    assert manager.check_sync_status()["is_synced"]


def test_case_alias_between_local_and_remote_is_rejected(manager, host, make_image):
    path = make_image("meme.png")
    host.objects["Meme.png"] = path.read_bytes()
    with pytest.raises(ValueError, match="casing"):
        manager.check_sync_status()


def test_small_valid_image_is_accepted_by_atomic_writer(tmp_path, image_bytes):
    data = image_bytes()
    assert len(data) < 1000
    target = tmp_path / "meme.png"
    save_image_stream([data], target, len(data))
    assert target.read_bytes() == data


@pytest.mark.parametrize("damage", ["truncated", "html", "checksum", "stream_error"])
def test_atomic_writer_retains_existing_image_on_failure(tmp_path, image_bytes, damage):
    target = tmp_path / "meme.png"
    original = image_bytes()
    target.write_bytes(original)
    data = image_bytes(96)

    def chunks():
        yield data[:5]
        raise OSError("connection lost")

    values = (
        chunks()
        if damage == "stream_error"
        else [
            data[:-3]
            if damage == "truncated"
            else b"<html>error</html>"
            if damage == "html"
            else data
        ]
    )
    with pytest.raises((ValueError, OSError)):
        save_image_stream(
            values,
            target,
            len(data) if damage == "truncated" else None,
            "incorrect" if damage == "checksum" else "",
        )
    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_fingerprint_is_content_based(make_image):
    path = make_image()
    assert (
        file_fingerprint(path)["sha256"]
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )


def test_remote_size_statistics_are_exact_when_available(manager, host, image_bytes):
    host.objects["remote.png"] = image_bytes()
    status = manager.check_sync_status()
    assert status["remote_total_bytes"] == len(image_bytes())
    assert status["remote_size_source"] == "exact"


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"size": 12}, 12),
        ({"file_size": 3.8}, 3),
        ({"bytes": "9"}, 9),
        ({"size": -1}, None),
        ({"size": "unknown"}, None),
    ],
)
def test_extract_remote_size(info, expected):
    assert SyncManager._extract_remote_size(info) == expected


def test_remote_root_disappearing_before_cleanup_preserves_local(
    manager, host, make_image
):
    local = make_image()
    inventories = iter([True, False])

    def list_images():
        host.listing_exists = next(inventories)
        return []

    host.get_image_list = list_images
    assert not manager.overwrite_from_remote()
    assert local.is_file()
    assert "disappeared" in manager.progress["message"]
