import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from astrbot_plugin_meme_manager.backend.auto_collect_index import AutoCollectImageIndex
from PIL import Image


def test_exact_match_reuses_unchanged_file_fingerprint(tmp_path):
    image_path = tmp_path / "meme.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    index = AutoCollectImageIndex()
    assert index.lookup(tmp_path, digest).exact_path == image_path
    with patch.object(Path, "open", side_effect=AssertionError("Unexpected file read")):
        assert index.lookup(tmp_path, digest).exact_path == image_path


def test_manifest_detects_rename_modification_and_deletion(tmp_path):
    image_path = tmp_path / "meme.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    index = AutoCollectImageIndex()
    assert index.lookup(tmp_path, digest).exact_path == image_path
    renamed = image_path.rename(tmp_path / "renamed.png")
    assert index.lookup(tmp_path, digest).exact_path == renamed
    Image.new("RGB", (32, 32), "blue").save(renamed)
    new_digest = hashlib.sha256(renamed.read_bytes()).hexdigest()
    assert index.lookup(tmp_path, digest).exact_path is None
    assert index.lookup(tmp_path, new_digest).exact_path == renamed
    renamed.unlink()
    assert index.lookup(tmp_path, new_digest).exact_path is None


def test_jpeg_pack_separation_and_bounded_cache(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    image_path = first / "meme.JPEG"
    Image.new("RGB", (16, 16), "red").save(image_path)
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    index = AutoCollectImageIndex(max_packs=1)
    assert index.lookup(first, digest).exact_path == image_path
    assert index.lookup(second, digest).exact_path is None
    assert len(index._packs) == 1
    assert index.lookup(first, digest).exact_path == image_path


def test_same_size_change_uses_nanosecond_mtime(tmp_path):
    image_path = tmp_path / "meme.png"
    image_path.write_bytes(b"old-image")
    old_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    index = AutoCollectImageIndex()
    assert index.lookup(tmp_path, old_digest).exact_path == image_path
    original_stat = image_path.stat()
    image_path.write_bytes(b"new-image")
    os.utime(
        image_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000),
    )
    new_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    assert index.lookup(tmp_path, old_digest).exact_path is None
    assert index.lookup(tmp_path, new_digest).exact_path == image_path


def test_visual_similarity_is_only_an_advisory(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    original = pack / "original.png"
    candidate = tmp_path / "candidate.png"
    image = Image.new("RGB", (32, 32))
    image.putdata([(x * 7, y * 7, 100) for y in range(32) for x in range(32)])
    image.save(original)
    image.resize((64, 64)).save(candidate)
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    index = AutoCollectImageIndex()
    with patch.object(
        index, "_difference_hash", side_effect=AssertionError("Unexpected decode")
    ):
        assert index.lookup(pack, digest).exact_path is None
    match = index.lookup(pack, digest, candidate)
    assert match.exact_path is None
    assert match.similar_path == original
    assert match.distance is not None and match.distance <= 5


def test_animated_files_are_excluded_from_visual_matches(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    animated = pack / "animated.gif"
    candidate = tmp_path / "candidate.png"
    Image.new("RGB", (16, 16), "red").save(
        animated,
        save_all=True,
        append_images=[Image.new("RGB", (16, 16), "blue")],
        duration=100,
        loop=0,
    )
    Image.new("RGB", (16, 16), "red").save(candidate)
    index = AutoCollectImageIndex()
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert index.lookup(pack, digest, candidate).similar_path is None
    animated_digest = hashlib.sha256(animated.read_bytes()).hexdigest()
    assert index.lookup(pack, animated_digest).exact_path == animated
    animated.unlink()
    Image.new("RGB", (16, 16), "red").save(pack / "static.png")
    # Animated candidates also skip visual matching against static pack images.
    Image.new("RGB", (16, 16), "red").save(
        animated,
        save_all=True,
        append_images=[Image.new("RGB", (16, 16), "blue")],
        duration=100,
    )
    assert index.lookup(pack, "different-digest", animated).similar_path is None


def test_symlink_outside_pack_is_ignored(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    outside = tmp_path / "outside.png"
    Image.new("RGB", (16, 16), "red").save(outside)
    try:
        (pack / "linked.png").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires privileges on this platform")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    assert AutoCollectImageIndex().lookup(pack, digest).exact_path is None
