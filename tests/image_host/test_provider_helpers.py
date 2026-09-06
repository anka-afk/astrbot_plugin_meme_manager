import hashlib
import io
import json

import pytest
import requests
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber
from image_host.providers.cloudflare_r2_provider import CloudflareR2Provider
from image_host.providers.stardots_provider import (
    AuthenticationError as StarDotsAuthenticationError,
)
from image_host.providers.stardots_provider import (
    InvalidResponseError as StarDotsInvalidResponseError,
)
from image_host.providers.stardots_provider import (
    NetworkError as StarDotsNetworkError,
)
from image_host.providers.stardots_provider import (
    RateLimitError,
    StarDotsProvider,
)
from image_host.providers.webdav_provider import (
    AuthenticationError,
    InvalidResponseError,
    WebDAVProvider,
)


@pytest.fixture
def response():
    def create(status=200, data=None, content=b"", headers=None):
        result = requests.Response()
        result.status_code = status
        result._content = json.dumps(data).encode() if data is not None else content
        result._content_consumed = True
        result.headers.update(headers or {})
        return result

    return create


@pytest.fixture
def r2(tmp_path):
    provider = CloudflareR2Provider(
        {
            "account_id": "account",
            "bucket_name": "bucket",
            "access_key_id": "test",
            "secret_access_key": "test",
            "local_dir": str(tmp_path),
            "public_url": "https://cdn.example/",
        }
    )
    yield provider
    provider.close()


@pytest.fixture
def webdav(tmp_path):
    provider = WebDAVProvider(
        {
            "url": "https://dav.example/root",
            "username": "user",
            "password": "pass",
            "local_dir": str(tmp_path),
            "base_path": "memes",
            "timeout": 10,
        }
    )
    yield provider
    provider.close()


@pytest.fixture
def stardots(tmp_path):
    provider = StarDotsProvider(
        {"key": "test", "secret": "test", "space": "space", "local_dir": str(tmp_path)}
    )
    yield provider
    provider.close()


def test_r2_preserves_root_nested_paths_and_encodes_urls(r2, make_image):
    root = make_image("root.png")
    nested = make_image("animals/cats/中文 #.png")
    assert r2._generate_s3_key(root) == "memes/root.png"
    assert r2._generate_s3_key(nested) == "memes/animals/cats/中文 #.png"
    assert r2._parse_s3_key("memes/animals/cats/file.png") == (
        "animals/cats",
        "file.png",
    )
    assert r2._get_public_url("memes/中文 #.png").endswith(
        "%E4%B8%AD%E6%96%87%20%23.png"
    )
    r2.public_url = ""
    assert r2._get_public_url("memes/root.png") == ""


@pytest.mark.parametrize(
    "key", ["outside/file.png", "memes/../file.png", "memes/C:/file.png"]
)
def test_r2_rejects_keys_outside_namespace(r2, key):
    with pytest.raises(ValueError):
        r2.delete_image(key)


def test_r2_streams_upload_with_correct_path_and_content_metadata(r2, make_image):
    path = make_image("animals/cats/meme.png")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with Stubber(r2.s3_client) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"version"'},
            {
                "Bucket": "bucket",
                "Key": "memes/animals/cats/meme.png",
                "Body": ANY,
                "ContentLength": path.stat().st_size,
                "ContentType": "image/png",
                "Metadata": {"sha256": digest},
            },
        )
        result = r2.upload_image(path)
        assert result["id"] == "memes/animals/cats/meme.png"
        assert result["relative_path"] == "animals/cats/meme.png"
        assert result["sha256"] == digest
        assert result["etag"] == '"version"'
        stub.assert_no_pending_responses()


def test_r2_lists_all_pages_and_filters_nonimages(r2):
    with Stubber(r2.s3_client) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "IsTruncated": True,
                "NextContinuationToken": "next",
                "Contents": [
                    {"Key": "memes/a.png", "Size": 10, "ETag": '"opaque-2"'},
                    {"Key": "memes/note.json", "Size": 20},
                ],
            },
            {"Bucket": "bucket", "Prefix": "memes/"},
        )
        stub.add_response(
            "list_objects_v2",
            {
                "IsTruncated": False,
                "Contents": [{"Key": "memes/cats/b.png", "Size": 30}],
            },
            {"Bucket": "bucket", "Prefix": "memes/", "ContinuationToken": "next"},
        )
        images = r2.get_image_list()
        assert [image["relative_path"] for image in images] == ["a.png", "cats/b.png"]
        assert images[0]["etag"] == '"opaque-2"'
        assert "sha256" not in images[0]


def test_r2_failed_second_page_does_not_return_partial_inventory(r2):
    with Stubber(r2.s3_client) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "IsTruncated": True,
                "NextContinuationToken": "next",
                "Contents": [{"Key": "memes/a.png", "Size": 10}],
            },
        )
        stub.add_client_error(
            "list_objects_v2", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(Exception):
            r2.get_image_list()


def test_r2_download_uses_conditional_get_and_closes_stream(r2, image_bytes, tmp_path):
    content = image_bytes()
    raw = io.BytesIO(content)
    with Stubber(r2.s3_client) as stub:
        stub.add_response(
            "get_object",
            {
                "Body": StreamingBody(raw, len(content)),
                "ContentLength": len(content),
                "Metadata": {"sha256": hashlib.sha256(content).hexdigest()},
            },
            {"Bucket": "bucket", "Key": "memes/a.png", "IfMatch": '"version"'},
        )
        path = tmp_path / "a.png"
        assert r2.download_image({"id": "memes/a.png", "etag": '"version"'}, path)
        assert path.read_bytes() == content
        assert raw.closed


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


def test_webdav_paths_preserve_legitimate_base_named_categories(webdav, tmp_path):
    assert webdav._get_remote_id(tmp_path / "memes" / "meme.png") == "memes/meme.png"
    assert webdav._remote_id_to_path("memes/meme.png") == "memes/memes/meme.png"
    assert webdav._strip_base_path("memes/memes/meme.png") == "memes/meme.png"
    assert webdav._url_for_path("memes/中文 #.png").endswith(
        "%E4%B8%AD%E6%96%87%20%23.png"
    )
    with pytest.raises(ValueError):
        webdav._get_remote_id(tmp_path.parent / "outside.png")


@pytest.mark.parametrize(
    "href",
    [
        "https://other.example/root/memes/a.png",
        "/root-other/memes/a.png",
        "/root/memes/%2e%2e/a.png",
        "/root/memes/CON.png",
    ],
)
def test_webdav_rejects_out_of_scope_and_unsafe_hrefs(webdav, href):
    with pytest.raises((ValueError, InvalidResponseError)):
        webdav._path_from_href(href, "memes")


def test_webdav_reads_only_successful_propstat_metadata(webdav):
    xml = """<D:multistatus xmlns:D="DAV:"><D:response><D:href>/root/memes/a.png</D:href>
    <D:propstat><D:prop><D:resourcetype/><D:getcontentlength>123</D:getcontentlength>
    <D:getetag>"version"</D:getetag></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
    <D:propstat><D:prop><D:getlastmodified/></D:prop><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat>
    </D:response></D:multistatus>"""
    assert webdav._parse_propfind_response(xml, "memes") == [
        {
            "path": "memes/a.png",
            "is_dir": False,
            "size": 123,
            "etag": '"version"',
            "modified": "",
        }
    ]


@pytest.mark.parametrize(
    "xml",
    [
        "not xml",
        "<html/>",
        '<D:multistatus xmlns:D="DAV:"/>',
        '<D:multistatus xmlns:D="DAV:"><D:response><D:href>/root/memes/a.png</D:href>'
        "<D:propstat><D:prop><D:resourcetype/></D:prop><D:status>HTTP/1.1 403 Forbidden</D:status>"
        "</D:propstat></D:response></D:multistatus>",
    ],
)
def test_webdav_rejects_malformed_or_denied_listings(webdav, xml):
    with pytest.raises(InvalidResponseError):
        webdav._parse_propfind_response(xml, "memes")


def test_webdav_missing_root_is_read_only(webdav, response, monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append(method)
        return response(404)

    monkeypatch.setattr(webdav.session, "request", request)
    assert webdav.get_image_list() == []
    assert calls == ["PROPFIND"]


def test_webdav_inaccessible_subtree_never_becomes_empty_inventory(
    webdav, response, monkeypatch
):
    root = b'<D:multistatus xmlns:D="DAV:"><D:response><D:href>/root/memes/cats/</D:href><D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>'
    responses = iter([response(207, content=root), response(403)])
    monkeypatch.setattr(
        webdav.session, "request", lambda *args, **kwargs: next(responses)
    )
    with pytest.raises(AuthenticationError):
        webdav.get_image_list()


def test_webdav_delete_keeps_base_named_category(webdav, response, monkeypatch):
    calls = []
    monkeypatch.setattr(
        webdav.session,
        "request",
        lambda method, url, **kwargs: calls.append((method, url)) or response(204),
    )
    assert webdav.delete_image("memes/meme.png")
    assert calls == [("DELETE", "https://dav.example/root/memes/memes/meme.png")]


def test_webdav_download_uses_version_and_keeps_original_on_truncation(
    webdav, response, monkeypatch, make_image, image_bytes
):
    path = make_image()
    before = path.read_bytes()
    calls = []

    def request(method, url, **kwargs):
        calls.append(kwargs)
        return response(content=image_bytes(96)[:-4])

    monkeypatch.setattr(webdav.session, "request", request)
    with pytest.raises(ValueError):
        webdav.download_image(
            {"id": "happy/meme.png", "size": len(before), "etag": '"version"'}, path
        )
    assert calls[0]["headers"] == {"If-Match": '"version"'}
    assert calls[0]["verify"] is True
    assert calls[0]["allow_redirects"] is False
    assert path.read_bytes() == before


def test_stardots_byte_size_takes_precedence_over_formatted_size(stardots):
    assert stardots._extract_image_size({"byteSize": 1536, "size": "1.5KB"}) == 1536
    assert stardots._extract_image_size({"size": "1.5KB"}) is None
    assert stardots._decode_category("") == ""
    assert stardots._encode_category("animals/cats") == "animals@@DIR@@cats"


@pytest.mark.parametrize("category", ["", "default", "animals/cats"])
def test_stardots_upload_list_download_delete_share_opaque_id(
    stardots, monkeypatch, response, make_image, image_bytes, category, tmp_path
):
    relative = f"{category}/meme.png" if category else "meme.png"
    path = make_image(relative)
    content = path.read_bytes()
    remote_name = (
        f"{category.replace('/', '@@DIR@@')}@@CAT@@meme.png" if category else "meme.png"
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/upload"):
            assert kwargs["files"]["file"][0] == remote_name
            return response(
                data={
                    "success": True,
                    "data": {
                        "filename": remote_name,
                        "url": "https://image.example/a?ticket=secret",
                    },
                }
            )
        if url.endswith("/list"):
            return response(
                data={
                    "success": True,
                    "data": {
                        "totalCount": 1,
                        "list": [
                            {
                                "name": remote_name,
                                "byteSize": len(content),
                                "uploadedAt": 123,
                                "url": "https://image.example/a?ticket=secret",
                            }
                        ],
                    },
                }
            )
        if url.endswith("/ticket"):
            assert kwargs["json"]["filename"] == remote_name
            return response(data={"success": True, "data": {"ticket": "temporary"}})
        if url.endswith("/delete"):
            assert kwargs["json"]["filenameList"] == [remote_name]
            return response(data={"success": True, "data": None})
        return response(content=content, headers={"Content-Length": str(len(content))})

    monkeypatch.setattr(stardots.session, "request", request)
    uploaded = stardots.upload_image(path)
    listed = stardots.get_image_list()[0]
    assert uploaded["id"] == listed["id"] == remote_name
    assert uploaded["relative_path"] == listed["relative_path"] == relative
    assert "ticket" not in listed["url"]
    downloaded = tmp_path / "downloaded.png"
    assert stardots.download_image(listed, downloaded)
    assert downloaded.read_bytes() == content
    assert stardots.delete_image(listed["id"])
    assert stardots.session.verify is True


def test_stardots_partial_pagination_is_never_returned(stardots, monkeypatch):
    first_page = [{"name": f"{number}.png", "byteSize": 10} for number in range(100)]

    def request(method, url, **kwargs):
        if kwargs["params"]["page"] == 1:
            return {"data": {"totalCount": 101, "list": first_page}}
        raise RateLimitError("Rate limit")

    monkeypatch.setattr(stardots, "_make_request", request)
    with pytest.raises(RateLimitError):
        stardots.get_image_list()


def test_stardots_checks_declared_total(stardots, monkeypatch):
    monkeypatch.setattr(
        stardots,
        "_make_request",
        lambda *args, **kwargs: {
            "data": {"totalCount": 2, "list": [{"name": "a.png", "byteSize": 10}]}
        },
    )
    with pytest.raises(StarDotsInvalidResponseError, match="incomplete"):
        stardots.get_image_list()


def test_stardots_tls_failure_never_disables_verification(stardots, monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        raise requests.exceptions.SSLError("untrusted certificate")

    monkeypatch.setattr(stardots.session, "request", request)
    with pytest.raises(StarDotsNetworkError, match="TLS"):
        stardots.get_image_list()
    assert len(calls) == 1
    assert stardots.session.verify is True


def test_stardots_auth_failure_does_not_retry(stardots, response, monkeypatch):
    calls = []
    monkeypatch.setattr(
        stardots.session,
        "request",
        lambda *args, **kwargs: calls.append(1) or response(401),
    )
    with pytest.raises(StarDotsAuthenticationError):
        stardots.get_image_list()
    assert calls == [1]


def test_stardots_retries_transient_errors_with_fresh_signatures(
    stardots, response, monkeypatch
):
    responses = iter(
        [
            response(429, headers={"Retry-After": "3"}),
            response(data={"success": True, "data": {}}),
        ]
    )
    headers = []
    delays = []

    def request(*args, **kwargs):
        headers.append(kwargs["headers"])
        return next(responses)

    monkeypatch.setattr(stardots.session, "request", request)
    monkeypatch.setattr(
        "image_host.providers.stardots_provider.time.sleep", delays.append
    )
    assert stardots._make_request("DELETE", stardots.base_url, json={})["success"]
    assert delays == [3]
    assert headers[0]["x-stardots-nonce"] != headers[1]["x-stardots-nonce"]


def test_stardots_download_error_does_not_expose_ticket(
    stardots, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        stardots,
        "_make_request",
        lambda *args, **kwargs: {"data": {"ticket": "private-ticket"}},
    )

    def request(*args, **kwargs):
        raise requests.ConnectionError("URL failed: ?ticket=private-ticket")

    monkeypatch.setattr(stardots.session, "get", request)
    with pytest.raises(StarDotsNetworkError) as error:
        stardots.download_image({"id": "meme.png"}, tmp_path / "meme.png")
    assert "private-ticket" not in str(error.value)
    assert "ConnectionError" in str(error.value)
