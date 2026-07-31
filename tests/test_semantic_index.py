from types import SimpleNamespace

import pytest
from backend import semantic_index as index
from backend.semantic_models import SemanticImage


class SyncEmbeddingProvider:
    provider_config = {"id": "provider-id", "embedding_model": "model-name"}

    @staticmethod
    def get_dim():
        return 2

    def __init__(self):
        self.calls = []

    def get_embedding(self, text):
        self.calls.append(text)
        return [3, 4]


class AsyncEmbeddingProvider(SyncEmbeddingProvider):
    async def get_embedding(self, text):
        self.calls.append(text)
        return [0, 2]

    async def get_embeddings(self, texts):
        return [[1, 0] for _ in texts]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), ("12", 12), (" 3 ", 3), (-1, None), (True, None), ("bad", None)],
)
def test_manifest_integer_parser(value, expected):
    assert index._manifest_int(value) == expected


def test_embedding_adapter_reads_metadata_and_readiness():
    provider = SyncEmbeddingProvider()
    adapter = index.EmbeddingAdapter(provider)
    assert adapter.provider_id == "provider-id"
    assert adapter.model_name == "model-name"
    assert adapter.dimension == 2
    assert adapter.ready
    assert adapter.signature == "provider-id:model-name:2"


def test_embedding_adapter_supports_meta_and_model_attributes():
    class Provider:
        model_name = "attribute-model"

        @staticmethod
        def meta():
            return SimpleNamespace(id="meta-id")

        @staticmethod
        def get_dim():
            return 3

        @staticmethod
        def get_embedding(text):
            return [1, 0, 0]

    adapter = index.EmbeddingAdapter(Provider())
    assert adapter.signature == "meta-id:attribute-model:3"


@pytest.mark.asyncio
async def test_embedding_adapter_normalizes_and_caches_queries():
    index._QUERY_VECTOR_CACHE.clear()
    provider = SyncEmbeddingProvider()
    adapter = index.EmbeddingAdapter(provider)
    assert await adapter.embed(" text ") == pytest.approx([0.6, 0.8])
    assert await adapter.embed("text") == pytest.approx([0.6, 0.8])
    assert provider.calls == ["text"]


@pytest.mark.asyncio
async def test_embedding_adapter_supports_async_batch_and_fallback():
    async_provider = AsyncEmbeddingProvider()
    async_adapter = index.EmbeddingAdapter(async_provider)
    assert await async_adapter.embed("text", use_cache=False) == [0, 1]
    assert await async_adapter.embed_many(["a", "b"]) == [[1, 0], [1, 0]]

    sync_provider = SyncEmbeddingProvider()
    sync_adapter = index.EmbeddingAdapter(sync_provider)
    assert await sync_adapter.embed_many(["a", "b"]) == [
        pytest.approx([0.6, 0.8]),
        pytest.approx([0.6, 0.8]),
    ]


@pytest.mark.asyncio
async def test_embedding_adapter_rejects_missing_or_invalid_provider_results():
    with pytest.raises(RuntimeError):
        await index.EmbeddingAdapter(None).embed("text")

    provider = SyncEmbeddingProvider()
    provider.get_embedding = lambda text: [1, 2, 3]
    with pytest.raises(RuntimeError, match="维度不一致"):
        await index.EmbeddingAdapter(provider).embed("text", use_cache=False)

    provider.get_embeddings = lambda texts: "invalid"
    with pytest.raises(RuntimeError, match="格式无效"):
        await index.EmbeddingAdapter(provider).embed_many(["text"])


def test_index_paths_validate_pack_id_and_manifest_loading(tmp_path):
    assert index.index_dir(tmp_path, "pack-id") == (
        tmp_path.resolve() / "semantic_indexes" / "pack-id"
    )
    with pytest.raises(ValueError):
        index.index_dir(tmp_path, "../bad")
    assert index.load_index_manifest(tmp_path, "pack-id") == {}
    manifest_path = index.index_manifest_path(tmp_path, "pack-id")
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"item_count": 1}', encoding="utf-8")
    assert index.load_index_manifest(tmp_path, "pack-id") == {"item_count": 1}
    manifest_path.write_text("not-json", encoding="utf-8")
    assert index.load_index_manifest(tmp_path, "pack-id") == {}


def test_index_file_accepts_only_generated_snapshot_names(tmp_path):
    valid = "index-" + "a" * 32 + ".faiss"
    assert index._index_file(tmp_path, "pack-id", {"index_file": valid}).name == valid
    assert (
        index._index_file(tmp_path, "pack-id", {"index_file": "../escape.faiss"}).name
        == "index.faiss"
    )


def test_manifest_writer_and_snapshot_cleanup(tmp_path):
    index._write_manifest(tmp_path, "pack-id", {"item_count": 1})
    assert index.load_index_manifest(tmp_path, "pack-id") == {"item_count": 1}
    root = index.index_dir(tmp_path, "pack-id")
    active = "index-" + "0" * 32 + ".faiss"
    (root / active).write_bytes(b"active")
    names = []
    for number in range(4):
        name = f"index-{number + 1:032x}.faiss"
        (root / name).write_bytes(str(number).encode())
        names.append(name)
    index._cleanup_old_index_snapshots(tmp_path, "pack-id", active)
    remaining = {path.name for path in root.glob("index-*.faiss")}
    assert active in remaining
    assert len(remaining) == 3


def build_ready_item():
    image = SemanticImage(
        content_sha256="a" * 64,
        relative_path="memes/happy/meme.png",
        category="happy",
        caption="猫在笑",
        tags=["猫"],
        caption_status="done",
        embedding_status="done",
    )
    image.category_review_status = "auto_match"
    image.category_review_context_hash = image.category_context_hash
    image.text_hash = index.text_hash(image.vector_text)
    return image.to_dict()


def test_index_is_ready_validates_manifest_index_and_metadata(tmp_path, monkeypatch):
    item = build_ready_item()
    digest = "entry-id"
    manifest = {
        "index_format": index.INDEX_FORMAT,
        "metadata_schema_version": index.SCHEMA_VERSION,
        "item_count": 1,
        "embedding_dimension": 2,
        "embedding_provider_id": "provider",
        "embedding_model": "model",
        "items": {digest: {"faiss_id": 1, "text_hash": item["text_hash"]}},
    }
    monkeypatch.setattr(index, "load_index_manifest", lambda *args: manifest)
    monkeypatch.setattr(
        index, "_read_faiss_index", lambda *args: SimpleNamespace(ntotal=1, d=2)
    )
    metadata = {"images": {digest: item}}
    assert index.index_is_ready(
        tmp_path,
        "pack-id",
        metadata,
        embedding_provider_id="provider",
        embedding_model="model",
        embedding_dimension=2,
    )
    assert not index.index_is_ready(
        tmp_path, "pack-id", metadata, embedding_provider_id="other"
    )
    manifest["item_count"] = 0
    assert not index.index_is_ready(tmp_path, "pack-id", metadata)


def test_reconstruct_reusable_vectors_matches_digest_or_text_hash():
    class Vector:
        def __init__(self, values):
            self.values = values

        def tolist(self):
            return self.values

    class OldIndex:
        @staticmethod
        def reconstruct(faiss_id):
            return Vector([3, 4])

    manifest = {
        "index_format": index.INDEX_FORMAT,
        "metadata_schema_version": index.SCHEMA_VERSION,
        "items": {
            "old": {"faiss_id": 1, "text_hash": "same"},
        },
    }
    vectors = index._reconstruct_reusable_vectors(
        OldIndex(), manifest, [("new", {"text_hash": "same"})]
    )
    assert vectors["new"] == pytest.approx([0.6, 0.8])
    assert index._reconstruct_reusable_vectors(None, manifest, []) == {}
