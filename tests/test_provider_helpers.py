import json
from pathlib import Path

import pytest
import requests
from image_host.providers.cloudflare_r2_provider import CloudflareR2Provider
from image_host.providers.stardots_provider import StarDotsProvider
from image_host.providers.webdav_provider import (
    AuthenticationError,
    InvalidResponseError,
    NetworkError,
    WebDAVProvider,
)


def test_r2_key_category_parsing_and_public_urls(tmp_path):
    provider = object.__new__(CloudflareR2Provider)
    provider.public_url = "https://cdn.example/"
    provider.bucket_name = "bucket"
    provider.account_id = "account"

    file_path = tmp_path / "happy" / "meme.png"
    file_path.parent.mkdir()
    assert provider._get_category_from_path(file_path) == "happy"
    assert provider._generate_s3_key(file_path) == "memes/happy/meme.png"
    assert provider._parse_s3_key("memes/happy/meme.png") == ("happy", "meme.png")
    assert provider._parse_s3_key("memes/meme.png") == ("", "meme.png")
    assert provider._get_public_url("memes/happy/meme.png") == (
        "https://cdn.example/memes/happy/meme.png"
    )
    provider.public_url = ""
    assert provider._get_public_url("memes/meme.png") == (
        "https://bucket.account.r2.dev/memes/meme.png"
    )


def test_stardots_category_size_rate_limit_and_cache_helpers():
    provider = object.__new__(StarDotsProvider)
    assert provider._encode_category("") == ""
    assert provider._encode_category("animals/cats") == "animals@@DIR@@cats"
    assert provider._decode_category("animals@@DIR@@cats") == "animals/cats"
    assert provider._decode_category("") == provider.DEFAULT_CATEGORY
    assert provider._extract_image_size({"fileSize": "42"}) == 42
    assert provider._extract_image_size({"bytes": 12.8}) == 12
    assert provider._extract_image_size({"size": "unknown"}) is None
    assert provider._is_rate_limit_error("Too Many Requests")
    assert provider._is_rate_limit_error("请求频率已超限")
    assert not provider._is_rate_limit_error("invalid timestamp")
    provider._image_list_cache = {"images": []}
    provider._invalidate_image_list_cache()
    assert provider._image_list_cache is None


def test_stardots_upload_records_load_save_and_corruption(tmp_path):
    provider = object.__new__(StarDotsProvider)
    provider.records_file = tmp_path / "records.json"
    provider._upload_records = {"meme.png": {"category": "happy"}}
    provider._save_records()
    assert json.loads(provider.records_file.read_text(encoding="utf-8")) == {
        "meme.png": {"category": "happy"}
    }
    provider._upload_records = {}
    provider._load_records()
    assert provider._upload_records == {"meme.png": {"category": "happy"}}
    provider.records_file.write_text("not-json", encoding="utf-8")
    provider._load_records()
    assert provider._upload_records == {}


@pytest.fixture
def webdav(tmp_path):
    provider = object.__new__(WebDAVProvider)
    provider.base_url = "https://dav.example/root"
    provider.public_url = "https://public.example"
    provider.base_path = "memes"
    provider.local_dir = tmp_path
    provider.timeout = 10
    provider.verify_ssl = True
    return provider


def test_webdav_path_and_url_helpers(webdav, tmp_path):
    assert webdav._normalize_path("/a\\b/") == "a/b"
    assert webdav._normalize_path(None) == ""
    assert webdav._url_for_path("") == "https://dav.example/root"
    assert webdav._url_for_path("memes/中文 图.png") == (
        "https://dav.example/root/memes/%E4%B8%AD%E6%96%87%20%E5%9B%BE.png"
    )
    assert webdav._remote_id_to_path("happy/meme.png") == "memes/happy/meme.png"
    assert webdav._strip_base_path("memes/happy/meme.png") == "happy/meme.png"
    assert webdav._strip_base_path("memes") == ""
    assert webdav._public_url_for_id("happy/中文.png").endswith(
        "happy/%E4%B8%AD%E6%96%87.png"
    )
    local_file = tmp_path / "happy" / "meme.png"
    assert webdav._get_remote_id(local_file) == "happy/meme.png"
    assert webdav._get_remote_id(Path("C:/outside/meme.png")) == "meme.png"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        ("false", False),
        ("否", False),
        ("yes", True),
        (0, False),
    ],
)
def test_webdav_boolean_parser(webdav, value, expected):
    assert webdav._parse_bool(value) is expected


def test_webdav_parse_propfind_response(webdav):
    xml = """<?xml version="1.0"?>
    <D:multistatus xmlns:D="DAV:">
      <D:response>
        <D:href>/root/memes/happy/</D:href>
        <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat>
      </D:response>
      <D:response>
        <D:href>/root/memes/happy/meme.png</D:href>
        <D:propstat><D:prop><D:resourcetype/><D:getcontentlength>123</D:getcontentlength></D:prop></D:propstat>
      </D:response>
    </D:multistatus>"""
    entries = webdav._parse_propfind_response(xml, "memes/happy")
    assert entries == [
        {"path": "memes/happy", "is_dir": True, "size": 0},
        {"path": "memes/happy/meme.png", "is_dir": False, "size": 123},
    ]
    with pytest.raises(InvalidResponseError):
        webdav._parse_propfind_response("not xml", "memes")


def test_webdav_ensure_remote_directories_accepts_created_and_existing(webdav):
    statuses = iter([201, 405])
    calls = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    def request(method, url):
        calls.append((method, url))
        return Response(next(statuses))

    webdav._request = request
    webdav._ensure_remote_dirs("memes/happy")
    assert calls == [
        ("MKCOL", "https://dav.example/root/memes"),
        ("MKCOL", "https://dav.example/root/memes/happy"),
    ]


def test_webdav_ensure_remote_directories_rejects_failure(webdav):
    class Response:
        status_code = 500

    webdav._request = lambda method, url: Response()
    with pytest.raises(InvalidResponseError):
        webdav._ensure_remote_dirs("memes")


def test_webdav_request_adds_defaults_and_maps_errors(webdav):
    class Response:
        status_code = 200

    class Session:
        def __init__(self):
            self.kwargs = None

        def request(self, method, url, **kwargs):
            self.kwargs = kwargs
            return Response()

    session = Session()
    webdav.session = session
    assert webdav._request("GET", "https://dav.example") is not None
    assert session.kwargs == {"timeout": 10, "verify": True}

    class AuthSession:
        @staticmethod
        def request(method, url, **kwargs):
            response = Response()
            response.status_code = 401
            return response

    webdav.session = AuthSession()
    with pytest.raises(AuthenticationError):
        webdav._request("GET", "https://dav.example")

    class FailedSession:
        @staticmethod
        def request(method, url, **kwargs):
            raise requests.RequestException("offline")

    webdav.session = FailedSession()
    with pytest.raises(NetworkError):
        webdav._request("GET", "https://dav.example")
