import json
import re
from pathlib import Path

import pytest
import utils
from image_host.core.upload_tracker import UploadTracker


def test_json_helpers_round_trip_unicode_and_default(tmp_path):
    target = tmp_path / "nested" / "data.json"
    assert utils.save_json({"message": "开心"}, str(target))
    assert utils.load_json(str(target)) == {"message": "开心"}
    assert utils.load_json(str(tmp_path / "missing.json"), {"fallback": True}) == {
        "fallback": True
    }


def test_save_json_reports_write_failure(tmp_path, monkeypatch):
    def fail_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", fail_open)
    assert not utils.save_json({"value": 1}, str(tmp_path / "data.json"))


def test_dictionary_probability_and_secret_helpers(monkeypatch):
    assert utils.dict_to_string({"a": 1, "b": 2}) == "a - 1\n\nb - 2\n"
    assert utils.normalize_probability("20") == 20
    assert utils.normalize_probability(-1) == 0
    assert utils.normalize_probability(101) == 100
    assert utils.normalize_probability("bad") == 0
    assert not utils.probability_hit(0, roll=1)
    assert utils.probability_hit(100, roll=100)
    assert utils.probability_hit(50, roll=50)
    assert not utils.probability_hit(50, roll=51)
    monkeypatch.setattr(utils.random, "randint", lambda start, end: 25)
    assert utils.probability_hit(30)
    secret = utils.generate_secret_key(24)
    assert len(secret) == 24
    assert re.fullmatch(r"[A-Za-z0-9]{24}", secret)


@pytest.mark.asyncio
async def test_get_public_ip_skips_failures_and_invalid_values(monkeypatch):
    class Response:
        def __init__(self, status, text):
            self.status = status
            self.value = text

        async def __aenter__(self):
            if isinstance(self.value, Exception):
                raise self.value
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def text(self):
            return self.value

    class Session:
        def __init__(self):
            self.responses = iter(
                [
                    Response(500, "error"),
                    Response(200, "not-an-ip"),
                    Response(200, " 1.2.3.4 \n"),
                ]
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def get(self, url, timeout):
            return next(self.responses)

    monkeypatch.setattr(utils.aiohttp, "ClientSession", Session)
    assert await utils.get_public_ip() == "1.2.3.4"


@pytest.mark.asyncio
async def test_get_public_ip_returns_placeholder_when_all_requests_fail(monkeypatch):
    class Response:
        async def __aenter__(self):
            raise OSError("offline")

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def get(self, url, timeout):
            return Response()

    monkeypatch.setattr(utils.aiohttp, "ClientSession", Session)
    assert await utils.get_public_ip() == "[服务器公网ip]"


def test_upload_tracker_persists_marks_removals_and_clear(tmp_path):
    tracker_path = tmp_path / "state" / "tracker.json"
    image_path = tmp_path / "meme.png"
    image_path.write_bytes(b"image")
    tracker = UploadTracker(tracker_path)
    assert tracker.get_uploaded_count() == 0
    assert not tracker.is_uploaded(image_path, "happy")

    tracker.mark_uploaded(image_path, "happy", "https://example/meme.png")
    assert tracker.is_uploaded(image_path, "happy")
    assert tracker.get_uploaded_count() == 1
    saved = json.loads(tracker_path.read_text(encoding="utf-8"))
    assert saved[str(Path("happy") / "meme.png")]["file_size"] == 5

    reloaded = UploadTracker(tracker_path)
    assert reloaded.is_uploaded(image_path, "happy")
    reloaded.remove_record(image_path, "happy")
    assert not reloaded.is_uploaded(image_path, "happy")
    reloaded.mark_uploaded(tmp_path / "missing.png")
    assert reloaded.uploaded_files["missing.png"]["file_size"] == 0
    reloaded.clear_record()
    assert reloaded.get_uploaded_count() == 0


def test_upload_tracker_recovers_from_corrupt_file(tmp_path):
    tracker_path = tmp_path / "tracker.json"
    tracker_path.write_text("not-json", encoding="utf-8")
    tracker = UploadTracker(tracker_path)
    assert tracker.uploaded_files == {}


def test_upload_tracker_swallows_save_errors(tmp_path, monkeypatch):
    tracker = UploadTracker(tmp_path / "tracker.json")

    def fail_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", fail_open)
    tracker.uploaded_files = {"meme.png": {}}
    tracker.save()
