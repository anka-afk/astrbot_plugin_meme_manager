"""Exercise the real MinIO SDK against a local S3-compatible HTTP server."""

import hashlib
import os
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from xml.sax.saxutils import escape

import pytest
from image_host.img_sync import ImageSync
from image_host.providers import cloudflare_r2_provider as r2_module
from minio import Minio
from minio.error import S3Error
from urllib3.exceptions import MaxRetryError


@pytest.fixture
def r2_server(tmp_path, monkeypatch):
    objects = {}
    uploads = {}
    requests = []
    failures = {"get": 0, "list_page": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, status, body=b"", headers=None):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/xml")
            self.send_header("x-amz-request-id", "fixture-request")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def error(self, status, code):
            self.reply(
                status,
                f"<Error><Code>{code}</Code><Message>Fixture error</Message></Error>".encode(),
            )

        def record(self):
            parsed = urlsplit(self.path)
            key = unquote(parsed.path).removeprefix("/bucket/")
            query = parse_qs(parsed.query, keep_blank_values=True)
            requests.append((self.command, key, query, dict(self.headers)))
            return key, query

        def do_PUT(self):
            key, query = self.record()
            content = self.rfile.read(int(self.headers["Content-Length"]))
            etag = '"' + hashlib.md5(content).hexdigest() + '"'
            if "uploadId" in query:
                upload = uploads[query["uploadId"][0]]
                upload["parts"][int(query["partNumber"][0])] = content
            else:
                objects[key] = {
                    "content": content,
                    "etag": etag,
                    "sha256": self.headers.get("x-amz-meta-sha256", ""),
                }
            self.reply(200, headers={"ETag": etag})

        def do_POST(self):
            key, query = self.record()
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if "uploads" in query:
                uploads["fixture-upload"] = {
                    "parts": {},
                    "sha256": self.headers.get("x-amz-meta-sha256", ""),
                }
                self.reply(
                    200,
                    (
                        "<InitiateMultipartUploadResult><Bucket>bucket</Bucket>"
                        f"<Key>{escape(key)}</Key><UploadId>fixture-upload</UploadId>"
                        "</InitiateMultipartUploadResult>"
                    ).encode(),
                )
                return
            upload = uploads.pop(query["uploadId"][0])
            part_numbers = [
                int(node.text)
                for node in ET.fromstring(body).iter()
                if node.tag.rsplit("}", 1)[-1] == "PartNumber"
            ]
            parts = [upload["parts"][number] for number in part_numbers]
            digest = hashlib.md5(b"".join(hashlib.md5(part).digest() for part in parts))
            etag = f'"{digest.hexdigest()}-{len(parts)}"'
            objects[key] = {
                "content": b"".join(parts),
                "etag": etag,
                "sha256": upload["sha256"],
            }
            self.reply(
                200,
                (
                    "<CompleteMultipartUploadResult><Bucket>bucket</Bucket>"
                    f"<Key>{escape(key)}</Key><ETag>{etag}</ETag>"
                    "</CompleteMultipartUploadResult>"
                ).encode(),
            )

        def do_GET(self):
            key, query = self.record()
            if "list-type" in query:
                offset = int(query.get("continuation-token", ["0"])[0])
                if offset and failures["list_page"]:
                    self.error(403, "AccessDenied")
                    return
                keys = sorted(
                    name
                    for name in objects
                    if name.startswith(query.get("prefix", [""])[0])
                )
                parts = [
                    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                    "<Name>bucket</Name><EncodingType>url</EncodingType>",
                ]
                for name in keys[offset : offset + 1]:
                    item = objects[name]
                    parts.append(
                        f"<Contents><Key>{quote(name, safe='')}</Key>"
                        f"<Size>{len(item['content'])}</Size>"
                        f"<ETag>{item['etag']}</ETag></Contents>"
                    )
                more = offset + 1 < len(keys)
                parts.append(f"<IsTruncated>{str(more).lower()}</IsTruncated>")
                if more:
                    parts.append(
                        f"<NextContinuationToken>{offset + 1}</NextContinuationToken>"
                    )
                parts.append("</ListBucketResult>")
                self.reply(200, "".join(parts).encode())
                return
            if failures["get"]:
                failures["get"] -= 1
                self.error(503, "SlowDown")
                return
            if key not in objects:
                self.error(404, "NoSuchKey")
                return
            item = objects[key]
            if self.headers.get("If-Match", item["etag"]) != item["etag"]:
                self.error(412, "PreconditionFailed")
                return
            self.reply(
                200,
                item["content"],
                {"ETag": item["etag"], "x-amz-meta-sha256": item["sha256"]},
            )

        def do_DELETE(self):
            key, query = self.record()
            if "uploadId" in query:
                uploads.pop(query["uploadId"][0], None)
            else:
                objects.pop(key, None)
            self.reply(204)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def client(endpoint, **kwargs):
        assert endpoint == "account.r2.cloudflarestorage.com"
        assert kwargs["secure"] is True
        assert kwargs["region"] == "auto"
        return Minio(f"127.0.0.1:{server.server_port}", **{**kwargs, "secure": False})

    monkeypatch.setattr(r2_module, "Minio", client)
    config = {
        "account_id": "account",
        "bucket_name": "bucket",
        "access_key_id": "fixture-access",
        "secret_access_key": "fixture-secret",
        "local_dir": str(tmp_path),
        "public_url": "https://cdn.example",
    }
    try:
        yield config, objects, uploads, requests, failures
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_r2_signed_round_trip_and_existing_sync_baselines(
    r2_server, tmp_path, make_image
):
    config, objects, uploads, requests, _ = r2_server
    nested = make_image("animals/cats/中文 #.png")
    root = make_image("root.png", 64)
    sync = ImageSync(config, tmp_path, "cloudflare_r2")
    try:
        assert requests == []
        assert sync.sync_manager.run("upload")
        assert (
            objects["memes/animals/cats/中文 #.png"]["content"] == nested.read_bytes()
        )
        assert objects["memes/root.png"]["content"] == root.read_bytes()
        assert sync.check_status()["is_synced"]
        records = sync.upload_tracker.get_uploaded_files()
        assert records["root.png"]["etag"] == objects["memes/root.png"]["etag"]
        assert records["root.png"]["etag"].startswith('"')

        listed = sync.provider.get_image_list()
        image = next(
            item for item in listed if item["relative_path"].startswith("animals/")
        )
        assert image["url"].endswith("animals/cats/%E4%B8%AD%E6%96%87%20%23.png")
        downloaded = tmp_path / "download.png"
        assert sync.provider.download_image(image, downloaded)
        assert downloaded.read_bytes() == nested.read_bytes()
        assert sync.provider.delete_image("memes/root.png")
        assert "memes/root.png" not in objects
        assert not uploads
        assert any("continuation-token" in query for _, _, query, _ in requests)
        assert all(
            "/auto/s3/aws4_request" in headers["Authorization"]
            for _, _, _, headers in requests
        )
        assert all("location" not in query for _, _, query, _ in requests)
    finally:
        sync.close()


def test_r2_multipart_keeps_full_object_checksum(r2_server, tmp_path, image_bytes):
    config, objects, uploads, requests, _ = r2_server
    content = image_bytes() + b"\0" * (5 * 1024 * 1024)
    source = tmp_path / "large.png"
    source.write_bytes(content)
    provider = r2_module.CloudflareR2Provider(config)
    try:
        result = provider.upload_image(source)
        assert objects["memes/large.png"]["content"] == content
        assert (
            objects["memes/large.png"]["sha256"] == hashlib.sha256(content).hexdigest()
        )
        assert result["etag"].endswith('-2"')
        assert not uploads
        assert sum("partNumber" in query for _, _, query, _ in requests) == 2
        target = tmp_path / "download.png"
        assert provider.download_image(result, target)
        assert target.read_bytes() == content
    finally:
        provider.close()


def test_r2_retries_transient_get_but_rejects_changed_object(
    r2_server, tmp_path, make_image
):
    config, objects, _, requests, failures = r2_server
    source = make_image()
    provider = r2_module.CloudflareR2Provider(config)
    try:
        image = provider.upload_image(source)
        failures["get"] = 2
        target = tmp_path / "download.png"
        assert provider.download_image(image, target)
        assert sum(method == "GET" for method, _, _, _ in requests) == 3
        before = target.read_bytes()
        objects[image["id"]]["etag"] = '"changed"'
        with pytest.raises(S3Error, match="PreconditionFailed"):
            provider.download_image(image, target)
        assert target.read_bytes() == before
        assert sum(method == "GET" for method, _, _, _ in requests) == 4
    finally:
        provider.close()


def test_r2_partial_inventory_cannot_authorize_deletion(
    r2_server, tmp_path, make_image
):
    config, _, _, requests, failures = r2_server
    first = make_image("first.png")
    second = make_image("second.png")
    sync = ImageSync(config, tmp_path, "cloudflare_r2")
    try:
        sync.provider.upload_image(first)
        sync.provider.upload_image(second)
        failures["list_page"] = True
        assert not sync.sync_manager.run("overwrite_from_remote")
        assert first.exists() and second.exists()
        assert all(method != "DELETE" for method, _, _, _ in requests)
    finally:
        sync.close()


def test_r2_stops_retrying_and_keeps_existing_download(r2_server, tmp_path, make_image):
    config, _, _, requests, failures = r2_server
    source = make_image()
    provider = r2_module.CloudflareR2Provider(config)
    try:
        image = provider.upload_image(source)
        failures["get"] = 10
        target = tmp_path / "download.png"
        target.write_bytes(b"existing image")
        with pytest.raises(MaxRetryError):
            provider.download_image(image, target)
        assert sum(method == "GET" for method, _, _, _ in requests) == 3
        assert target.read_bytes() == b"existing image"
    finally:
        provider.close()


def test_image_sync_imports_without_aws_sdk():
    plugin_dir = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        (str(plugin_dir), str(plugin_dir.parents[2]), env.get("PYTHONPATH", ""))
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys

class BlockAwsSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'boto3', 'botocore'}:
            raise ModuleNotFoundError(fullname, name=fullname)

sys.meta_path.insert(0, BlockAwsSdk())
import image_host.img_sync
assert 'boto3' not in sys.modules
assert 'botocore' not in sys.modules
""",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
