import math

import pytest
from backend import semantic_models as models

VALID_HASH = "a" * 64


def test_normalize_tags_accepts_string_and_removes_duplicates():
    assert models.normalize_tags(" happy ") == ["happy"]
    assert models.normalize_tags(["happy", "", None, "happy", 3]) == ["happy", "3"]
    assert models.normalize_tags({"one", "two"}) in (["one", "two"], ["two", "one"])
    assert models.normalize_tags(123) == []


def test_category_tag_helpers_replace_existing_category_tags():
    assert models.build_category_tag(" happy ") == "category:happy"
    assert models.build_category_tag("") == ""
    assert models.is_category_tag("分类:开心")
    assert models.is_category_tag(" category:happy ")
    assert not models.is_category_tag("happy")
    assert models.ensure_category_tag(
        ["category:old", "funny", "分类:旧", "funny"], "happy"
    ) == ["category:happy", "funny"]
    assert models.ensure_category_tag(["funny"], "") == ["funny"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("auto_match", True),
        ("needs_review", True),
        ("manual_confirmed", True),
        ("manual_rejected", True),
        ("unchecked", False),
        (None, False),
    ],
)
def test_category_review_completion(status, expected):
    assert models.category_review_is_complete(status) is expected


def test_category_analysis_currentness():
    assert not models.category_analysis_is_current(None)
    assert not models.category_analysis_is_current(
        {"category_review_status": "unchecked"}
    )
    assert models.category_analysis_is_current(
        {"category_review_status": "manual_confirmed", "manual_override": True}
    )
    assert models.category_analysis_is_current(
        {"category_review_status": "auto_match", "provenance": "manual"}
    )
    assert models.category_analysis_is_current(
        {
            "category_review_status": "auto_match",
            "prompt_version": models.PROMPT_VERSION,
        }
    )
    assert not models.category_analysis_is_current(
        {"category_review_status": "auto_match", "prompt_version": "old"}
    )


def test_completed_caption_remains_reusable_across_prompt_versions():
    item = {
        "caption_status": "done",
        "caption": "已有描述",
        "tags": ["已有标签"],
        "category_review_status": "unchecked",
        "prompt_version": "old",
    }
    assert models.semantic_caption_is_complete(item)
    assert not models.category_analysis_is_current(item)
    assert models.semantic_caption_is_complete({**item, "caption_status": "pending"})
    assert not models.semantic_caption_is_complete({**item, "caption_status": "failed"})
    assert not models.semantic_caption_is_complete({**item, "caption": ""})
    assert not models.semantic_caption_is_complete({**item, "tags": []})


def test_semantic_entry_id_is_stable_and_path_sensitive():
    first = models.semantic_entry_id(VALID_HASH, "happy", "happy/a.png")
    assert first == models.semantic_entry_id(
        VALID_HASH.upper(), "happy", "happy\\a.png"
    )
    assert first != models.semantic_entry_id(VALID_HASH, "happy", "happy/b.png")
    with pytest.raises(ValueError):
        models.semantic_entry_id("short", "happy")


def test_category_context_hash_normalizes_description_whitespace():
    first = models.category_context_hash(VALID_HASH, "happy", " 开心   快乐 ")
    second = models.category_context_hash(VALID_HASH, "happy", "开心 快乐")
    assert first == second
    assert first != models.category_context_hash(VALID_HASH, "sad", "开心 快乐")


def test_anchor_caption_to_category_only_for_non_conflicting_content():
    anchored = models.anchor_caption_to_category(
        "角色正在大笑", ["funny"], "happy", "match", "开心 用于回应"
    )
    assert anchored.startswith("以当前分类“happy”（开心 用于回应）")
    assert (
        models.anchor_caption_to_category("角色正在大笑", [], "happy", "conflict")
        == "角色正在大笑"
    )
    assert models.anchor_caption_to_category("", [], "happy", "match") == ""
    assert models.anchor_caption_to_category(anchored, [], "happy", "match") == anchored


def test_build_semantic_text_contains_category_once_and_content_tags():
    text = models.build_semantic_text(
        "猫在笑",
        ["category:old", "可爱", "可爱"],
        "哈哈",
        "happy",
        "表示开心",
    )
    assert "图片含义：猫在笑" in text
    assert "固定分类标签：category:happy" in text
    assert "分类含义：表示开心" in text
    assert "语义标签：可爱" in text
    assert text.count("category:happy") == 1


def test_text_hash_is_deterministic():
    assert models.text_hash("hello") == models.text_hash("hello")
    assert models.text_hash("hello") != models.text_hash("world")


def test_vector_normalization_and_cosine_similarity():
    vector = models.normalize_vector([3, 4])
    assert vector == pytest.approx([0.6, 0.8])
    assert math.sqrt(sum(value * value for value in vector)) == pytest.approx(1)
    assert models.cosine_similarity(vector, vector) == pytest.approx(1)
    assert models.cosine_similarity([1, 0], [0, 1]) == 0


@pytest.mark.parametrize("vector", [None, [], [0, 0], [float("nan")], [float("inf")]])
def test_vector_normalization_rejects_invalid_vectors(vector):
    with pytest.raises(ValueError):
        models.normalize_vector(vector)


def test_cosine_similarity_rejects_dimension_mismatch():
    with pytest.raises(ValueError):
        models.cosine_similarity([1], [1, 2])


def test_short_id_expands_after_prefix_collision():
    digest = "123456789abc" + "d" * 52
    assert models.short_id(digest) == "meme:123456789abc"
    assert models.short_id(digest, ["123456789abc"]) == "meme:123456789abcdddd"
    with pytest.raises(ValueError):
        models.short_id("short")


def test_build_id_map_uses_unique_prefixes():
    first = "123456789abc" + "0" * 52
    second = "123456789abc" + "1" * 52
    result = models.build_id_map([first, second, first])
    assert result[first] == "meme:" + first[:16]
    assert result[second] == "meme:" + second[:16]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("meme:123456789abc", "123456789abc"),
        (" meme:ABCDEF123456 ", "abcdef123456"),
        ("123456789abc", ""),
        ("meme:short", ""),
        ("meme:123456789abz", ""),
    ],
)
def test_parse_meme_id(value, expected):
    assert models.parse_meme_id(value) == expected


def test_extract_semantic_references_deduplicates_and_cleans_markers():
    text = (
        "正常回复\n"
        "&&meme:123456789abc&&\n"
        "meme:abcdef123456 候选图片说明。后续正文\n"
        "再次 meme:123456789abc"
    )
    cleaned, references = models.extract_and_clean_semantic_meme_references(text)
    assert references == ["meme:123456789abc", "meme:abcdef123456"]
    assert "meme:" not in cleaned
    assert "正常回复" in cleaned


def test_extract_visible_reply_removes_reasoning_and_machine_markers():
    text = "<think>内部推理</think>\n```json\n可见回复 &&happy&&\n```"
    assert models.extract_visible_semantic_reply(text) == "可见回复"


def test_compact_semantic_query_cleans_prefix_quotes_and_limits_length():
    assert models.compact_semantic_query('检索词： "开心   大笑。"') == "开心 大笑"
    assert len(models.compact_semantic_query("x" * 100, max_chars=10)) == 10
    assert len(models.compact_semantic_query("x" * 100, max_chars=1)) == 8


@pytest.mark.parametrize(
    ("value", "fallback", "expected"),
    [
        ('{"query":"开心 大笑"}', "备用", "开心 大笑"),
        ('前文 {"keywords":"猫猫"} 后文', "备用", "猫猫"),
        ("直接检索词", "备用", "直接检索词"),
        ("{}", "备用词", "备用词"),
        ("{bad json", "备用词", "备用词"),
    ],
)
def test_parse_semantic_query_result(value, fallback, expected):
    assert models.parse_semantic_query_result(value, fallback) == expected


def test_semantic_image_normalizes_fields_and_round_trips():
    image = models.SemanticImage(
        content_sha256=VALID_HASH.upper(),
        relative_path="happy\\meme.png",
        category=" happy ",
        category_description=" 开心 ",
        caption="猫在笑",
        tags=["category:old", "可爱", "可爱"],
        caption_status="invalid",
        embedding_status="invalid",
        category_fit="invalid",
        reclassification_history=[{"reason": "x" * 600}] * 25,
    )
    assert image.content_sha256 == VALID_HASH
    assert image.relative_path == "happy/meme.png"
    assert image.category == "happy"
    assert image.tags == ["category:happy", "可爱"]
    assert image.caption_status == "pending"
    assert image.embedding_status == "pending"
    assert image.category_fit == "uncertain"
    assert len(image.reclassification_history) == 20
    assert len(image.reclassification_history[0]["reason"]) == 500
    assert image.text_hash == models.text_hash(image.vector_text)
    assert models.SemanticImage.from_dict(image.to_dict()).to_dict() == image.to_dict()


def test_semantic_image_manual_override_uses_manual_fields():
    image = models.SemanticImage(
        content_sha256=VALID_HASH,
        relative_path="happy/meme.png",
        category="happy",
        caption="自动描述",
        tags=["自动"],
        visible_text="自动文字",
        manual_caption="人工描述",
        manual_tags=["人工", "人工"],
        manual_visible_text="人工文字",
        manual_override=True,
    )
    assert image.caption == "人工描述"
    assert image.tags == ["category:happy", "人工"]
    assert image.visible_text == "人工文字"


def test_parse_caption_result_supports_json_and_embedded_json():
    expected = ("猫在笑", ["可爱", "开心"], "哈哈")
    assert (
        models.parse_caption_result(
            {"caption": "猫在笑", "tags": ["可爱", "开心"], "visible_text": "哈哈"}
        )
        == expected
    )
    assert (
        models.parse_caption_result(
            '工具参数 {"foo":1} 最终 {"caption":"猫在笑","tags":["可爱","开心"],"visible_text":"哈哈"}'
        )
        == expected
    )


@pytest.mark.parametrize("value", [None, "not json", {}, {"caption": "x", "tags": []}])
def test_parse_caption_result_rejects_invalid_payloads(value):
    with pytest.raises(ValueError):
        models.parse_caption_result(value)


def test_parse_caption_result_with_review_normalizes_review_fields():
    result = models.parse_caption_result_with_review(
        {
            "caption": "猫在生气",
            "tags": ["猫", "生气"],
            "visible_text": "哼",
            "category_fit": "conflict",
            "category_review_reason": "  分类   不匹配  ",
            "suggested_category": " angry ",
        }
    )
    assert result == (
        "猫在生气",
        ["猫", "生气"],
        "哼",
        "conflict",
        "分类 不匹配",
        "angry",
    )


def test_parse_caption_result_with_review_handles_legacy_and_invalid_fit():
    legacy = models.parse_caption_result_with_review(
        {"caption": "猫", "tags": ["可爱"], "visible_text": ""}
    )
    assert legacy[3:] == ("uncertain", "模型未返回分类符合判断", "")
    with pytest.raises(ValueError):
        models.parse_caption_result_with_review(
            {"caption": "猫", "tags": ["可爱"], "category_fit": "invalid"}
        )
