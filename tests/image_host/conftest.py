import hashlib
import io

import pytest
from PIL import Image


@pytest.fixture
def image_bytes():
    def create(value=32):
        stream = io.BytesIO()
        Image.new("L", (2, 2), value).save(stream, "PNG")
        return stream.getvalue()

    return create


@pytest.fixture
def make_image(tmp_path, image_bytes):
    def create(relative="happy/meme.png", value=32):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image_bytes(value))
        return path

    return create


class MemoryHost:
    def __init__(self, root):
        self.root = root
        self.objects = {}
        self.calls = []
        self.list_calls = 0
        self.fail_list_at = 0
        self.fail_upload = False
        self.fail_download = False
        self.fail_delete = False
        self.after_upload = None
        self.after_download = None
        self.expose_checksum = False

    def metadata(self, relative):
        category, _, filename = relative.rpartition("/")
        content = self.objects[relative]
        info = {
            "id": "opaque:" + relative,
            "relative_path": relative,
            "filename": filename,
            "category": category,
            "size": len(content),
            "etag": hashlib.sha256(content).hexdigest(),
        }
        if self.expose_checksum:
            info["sha256"] = hashlib.sha256(content).hexdigest()
        return info

    def get_image_list(self):
        self.list_calls += 1
        if self.list_calls == self.fail_list_at:
            raise OSError("Listing interrupted")
        return [self.metadata(relative) for relative in self.objects]

    def upload_image(self, path):
        relative = path.relative_to(self.root).as_posix()
        self.calls.append(("upload", relative))
        if self.fail_upload:
            raise OSError("Upload interrupted")
        self.objects[relative] = path.read_bytes()
        if self.after_upload:
            self.after_upload()
        return self.metadata(relative)

    def download_image(self, info, path):
        relative = info["relative_path"]
        self.calls.append(("download", relative))
        path.write_bytes(
            self.objects[relative][:5] if self.fail_download else self.objects[relative]
        )
        if self.after_download:
            self.after_download()
        return not self.fail_download

    def delete_image(self, image_id):
        self.calls.append(("delete", image_id))
        if self.fail_delete:
            return False
        del self.objects[image_id.removeprefix("opaque:")]
        return True


@pytest.fixture
def host(tmp_path):
    return MemoryHost(tmp_path)
