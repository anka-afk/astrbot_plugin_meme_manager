"""Exercise real spawned workers against an isolated HTTP WebDAV fixture."""

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse
from xml.sax.saxutils import escape

import pytest
from image_host.img_sync import ImageSync


@pytest.fixture
def dav_server():
    objects = {}
    directories = {"/dav"}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, status, body=b"", headers=None):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_PROPFIND(self):
            path = unquote(urlparse(self.path).path).rstrip("/")
            requests.append(("PROPFIND", path))
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if path not in directories:
                self.reply(404)
                return
            entries = [path] + sorted(
                candidate
                for candidate in set(objects) | directories
                if str(PurePosixPath(candidate).parent) == path
            )
            parts = ['<D:multistatus xmlns:D="DAV:">']
            for candidate in entries:
                is_dir = candidate in directories
                props = (
                    "<D:resourcetype><D:collection/></D:resourcetype>"
                    if is_dir
                    else (
                        "<D:resourcetype/><D:getcontentlength>"
                        f"{len(objects[candidate])}</D:getcontentlength><D:getetag>"
                        f'"{hashlib.sha256(objects[candidate]).hexdigest()}"</D:getetag>'
                    )
                )
                parts.append(
                    f"<D:response><D:href>{escape(quote(candidate))}</D:href>"
                    f"<D:propstat><D:prop>{props}</D:prop>"
                    "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
                )
            parts.append("</D:multistatus>")
            self.reply(
                207, "".join(parts).encode(), {"Content-Type": "application/xml"}
            )

        def do_MKCOL(self):
            path = unquote(urlparse(self.path).path).rstrip("/")
            requests.append(("MKCOL", path))
            existed = path in directories
            directories.add(path)
            self.reply(405 if existed else 201)

        def do_PUT(self):
            path = unquote(urlparse(self.path).path)
            requests.append(("PUT", path))
            objects[path] = self.rfile.read(int(self.headers["Content-Length"]))
            self.reply(
                201, headers={"ETag": f'"{hashlib.sha256(objects[path]).hexdigest()}"'}
            )

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            requests.append(("GET", path))
            if path not in objects:
                self.reply(404)
                return
            etag = f'"{hashlib.sha256(objects[path]).hexdigest()}"'
            if self.headers.get("If-Match", etag) != etag:
                self.reply(412)
                return
            self.reply(200, objects[path], {"Content-Type": "image/png", "ETag": etag})

        def do_DELETE(self):
            path = unquote(urlparse(self.path).path)
            requests.append(("DELETE", path))
            objects.pop(path, None)
            self.reply(204)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = {
        "url": f"http://127.0.0.1:{server.server_port}/dav",
        "username": "fixture",
        "password": "fixture",
        "base_path": "memes",
        "timeout": 3,
    }
    try:
        yield config, objects, directories, requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_spawned_worker_round_trip_and_mirror(
    dav_server, tmp_path, make_image, image_bytes
):
    config, remote, directories, requests = dav_server
    nested = make_image("animals/cats/中文 #.png")
    root = make_image("root.png", 64)
    sync = ImageSync(config, tmp_path, "webdav")
    try:
        upload = sync.upload_to_remote()
        upload.join(timeout=20)
        assert not upload.is_alive()
        assert upload.exitcode == 0, sync.get_task_status()
        assert remote["/dav/memes/animals/cats/中文 #.png"] == nested.read_bytes()
        assert remote["/dav/memes/root.png"] == root.read_bytes()
        status = sync.get_task_status()
        assert status["success"] is True
        assert (status["processed"], status["total"], status["succeeded"]) == (2, 2, 2)
        assert sync.check_status()["is_synced"]

        remote["/dav/memes/root.png"] = image_bytes(96)
        download = sync.download_to_local()
        download.join(timeout=20)
        assert download.exitcode == 0, sync.get_task_status()
        assert root.read_bytes() == image_bytes(96)

        remote["/dav/memes/extra.png"] = image_bytes(128)
        mirror = sync._start_sync_process("overwrite_to_remote")
        mirror.join(timeout=20)
        assert mirror.exitcode == 0, sync.get_task_status()
        assert "/dav/memes/extra.png" not in remote
        assert ("DELETE", "/dav/memes/extra.png") in requests
    finally:
        sync.close()


def test_missing_remote_root_cannot_authorize_local_deletion(
    dav_server, tmp_path, make_image
):
    config, remote, directories, requests = dav_server
    image = make_image()
    sync = ImageSync(config, tmp_path, "webdav")
    try:
        process = sync._start_sync_process("overwrite_from_remote")
        process.join(timeout=20)
        assert process.exitcode == 1
        assert image.exists()
        assert sync.get_task_status()["phase"] == "failed"
        assert requests == [("PROPFIND", "/dav/memes")]
    finally:
        sync.close()
