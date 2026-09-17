"""Exercise Lsky's HTTP contract and sync lifecycle without a live account."""

import base64
import hashlib
import json
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from image_host.img_sync import ImageSync
from image_host.providers.lsky_provider import LskyProvider


@pytest.fixture
def lsky_server():
    state = {
        "images": {},
        "calls": [],
        "fail_page": False,
        "transform": False,
        "fail_upload": False,
        "fail_delete": False,
        "next_key": 1,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, data=None, code=200, content=None):
            body = (
                content
                if content is not None
                else json.dumps({"status": code == 200, "data": data}).encode()
            )
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            state["calls"].append(("GET", self.path))
            if url.path.startswith("/original/"):
                assert "Authorization" not in self.headers
                self.reply(
                    content=state["images"][url.path.rsplit("/", 1)[1]]["content"]
                )
                return
            assert self.headers.get("Authorization") == "Bearer fixture"
            query = parse_qs(url.query)
            page = int(query.get("page", [1])[0])
            if state["fail_page"] and page == 2:
                self.reply(code=503)
                return
            if url.path == "/api/v1/albums":
                items = [{"id": 1}, {"id": 2}, {"id": 3}]
            else:
                album = int(query.get("album_id", [0])[0])
                items = []
                for key, value in state["images"].items():
                    if value["album"] == album:
                        items.append(
                            {
                                "key": key,
                                "origin_name": value["name"],
                                "sha1": hashlib.sha1(value["content"]).hexdigest(),
                                "size": round(len(value["content"]) / 1024, 2),
                                "links": {"url": f"{address}/original/{key}"},
                            }
                        )
            self.reply(
                {
                    "data": items[(page - 1) * 2 : page * 2],
                    "current_page": page,
                    "last_page": max(1, (len(items) + 1) // 2),
                    "total": len(items),
                }
            )

        def do_POST(self):
            assert self.headers.get("Authorization") == "Bearer fixture"
            state["calls"].append(("POST", self.path))
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if state["fail_upload"]:
                self.reply(code=503)
                return
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
            )
            part = next(
                part
                for part in message.iter_parts()
                if part.get_param("name", header="content-disposition") == "file"
            )
            content = part.get_payload(decode=True)
            if state["transform"]:
                content += b"changed"
            key = f"key{state['next_key']}"
            state["next_key"] += 1
            state["images"][key] = {
                "name": part.get_filename(),
                "content": content,
                "album": 2,
            }
            self.reply(
                {
                    "key": key,
                    "origin_name": part.get_filename(),
                    "sha1": hashlib.sha1(content).hexdigest(),
                    "links": {"url": f"{address}/original/{key}"},
                }
            )

        def do_DELETE(self):
            assert self.headers.get("Authorization") == "Bearer fixture"
            state["calls"].append(("DELETE", self.path))
            if state["fail_delete"]:
                self.reply(code=503)
                return
            state["images"].pop(self.path.rsplit("/", 1)[1], None)
            self.reply()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": address, "token": "Bearer fixture", "namespace": "memes"}, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_spawned_sync_roundtrip_and_mirror(
    lsky_server, tmp_path, make_image, image_bytes
):
    config, state = lsky_server
    state["images"]["personal"] = {
        "name": "holiday.png",
        "content": image_bytes(10),
        "album": 2,
    }
    nested = make_image("开心/猫猫/中文 #.png")
    original = nested.read_bytes()
    root = make_image("root.png", 64)
    make_image("third.png", 80)
    sync = ImageSync(config, tmp_path, "lsky")
    try:
        process = sync.upload_to_remote()
        process.join(timeout=25)
        assert process.exitcode == 0, sync.get_task_status()
        assert len(state["images"]) == 4
        assert sync.check_status()["is_synced"]
        assert any("album_id=2&page=2" in path for method, path in state["calls"])
        uploaded = sync.provider.get_image_list()
        assert {image["relative_path"] for image in uploaded} == {
            "开心/猫猫/中文 #.png",
            "root.png",
            "third.png",
        }
        root_info = next(image for image in uploaded if image["filename"] == "root.png")
        root.write_bytes(image_bytes(96))
        process = sync._start_sync_process("overwrite_to_remote")
        process.join(timeout=25)
        assert process.exitcode == 0, sync.get_task_status()
        assert root_info["id"] not in state["images"]
        assert len(state["images"]) == 4
        nested.unlink()
        process = sync.download_to_local()
        process.join(timeout=25)
        assert process.exitcode == 0, sync.get_task_status()
        assert nested.read_bytes() == original
        root.unlink()
        process = sync._start_sync_process("overwrite_to_remote")
        process.join(timeout=25)
        assert process.exitcode == 0, sync.get_task_status()
        assert len(state["images"]) == 3
        assert "personal" in state["images"]
    finally:
        sync.close()


@pytest.mark.parametrize("failure", ["fail_upload", "transform", "fail_delete"])
def test_failed_replacement_keeps_original(
    lsky_server, tmp_path, make_image, image_bytes, failure
):
    config, state = lsky_server
    provider = LskyProvider({**config, "local_dir": tmp_path})
    try:
        path = make_image()
        old = provider.upload_image(path)
        old_content = path.read_bytes()
        path.write_bytes(image_bytes(99))
        state[failure] = True
        with pytest.raises(ValueError):
            provider.upload_image(path)
        assert state["images"][old["id"]]["content"] == old_content
        if failure != "fail_delete":
            assert len(state["images"]) == 1
        else:
            with pytest.raises(ValueError, match="duplicate managed paths"):
                provider.get_image_list()
    finally:
        provider.close()


def test_failed_listing_prevents_mirror_deletion(lsky_server, tmp_path, make_image):
    config, state = lsky_server
    path = make_image()
    state["fail_page"] = True
    sync = ImageSync(config, tmp_path, "lsky")
    try:
        process = sync._start_sync_process("overwrite_from_remote")
        process.join(timeout=25)
        assert process.exitcode == 1
        assert path.exists()
        assert not any(method == "DELETE" for method, _ in state["calls"])
    finally:
        sync.close()


def test_checksum_failure_preserves_download_target(
    lsky_server, tmp_path, make_image, image_bytes
):
    config, state = lsky_server
    provider = LskyProvider({**config, "local_dir": tmp_path})
    try:
        path = make_image()
        old_content = path.read_bytes()
        remote = provider.upload_image(path)
        state["images"][remote["id"]]["content"] = image_bytes(99)
        with pytest.raises(ValueError, match="checksum mismatch"):
            provider.download_image(remote, path)
        assert path.read_bytes() == old_content
        assert not list(path.parent.glob(".lsky-download-*"))
    finally:
        provider.close()


def test_idempotent_upload_and_namespace_isolation(lsky_server, tmp_path, make_image):
    config, state = lsky_server
    provider = LskyProvider({**config, "local_dir": tmp_path})
    other = LskyProvider({**config, "local_dir": tmp_path, "namespace": "memes__other"})
    try:
        path = make_image()
        first = provider.upload_image(path)
        assert provider.upload_image(path)["id"] == first["id"]
        assert len(state["images"]) == 1
        assert other.get_image_list() == []
        other.upload_image(path)
        assert len(provider.get_image_list()) == 1
        assert len(other.get_image_list()) == 1
    finally:
        provider.close()
        other.close()


def test_managed_path_traversal_rejected(lsky_server, tmp_path, image_bytes):
    config, state = lsky_server
    encoded = base64.urlsafe_b64encode(b"../escape.png").decode().rstrip("=")
    state["images"]["bad"] = {
        "name": f"amm1_5_memes__{encoded}.png",
        "content": image_bytes(),
        "album": 0,
    }
    provider = LskyProvider({**config, "local_dir": tmp_path})
    try:
        with pytest.raises(ValueError):
            provider.get_image_list()
    finally:
        provider.close()
