import random

import pytest
from astrbot_plugin_meme_manager.backend.meme_parser import MemeParser


@pytest.mark.parametrize(
    ("text", "visible", "selected"),
    [
        ("好的 &&happy&&！", "好的 ！", ["happy"]),
        ("[sad] &&happy&& (sad)", "  ", ["sad", "happy"]),
        ("&&happy&&&&sad&&", "", ["happy", "sad"]),
        ("&&自定义&&", "", ["自定义"]),
        (":happy:", "", ["happy"]),
        ("&&unknown&&", "", []),
        ("&&未闭合", "&&未闭合", []),
        ("&&&&", "&&&&", []),
        (r"\&&happy&&", r"\&&happy&&", []),
        ("`&&happy&&`", "`&&happy&&`", []),
        ("``a ` &&happy&&``", "``a ` &&happy&&``", []),
        ("`a\n&&happy&&\nb`", "`a\n&&happy&&\nb`", []),
        ("```python\n&&happy&&\n```\n&&sad&&", "```python\n&&happy&&\n```\n", ["sad"]),
        ("~~~\n&&happy&&\n~~~", "~~~\n&&happy&&\n~~~", []),
        ("    &&happy&&", "    &&happy&&", []),
        ("[happy](https://example.com)", "[happy](https://example.com)", []),
        ("[&&happy&&](https://example.com)", "[&&happy&&](https://example.com)", []),
        ("https://example.com/&&happy&&", "https://example.com/&&happy&&", []),
        (
            "<thinking>\n&&happy&&\n</thinking>\n&&sad&&",
            "<thinking>\n&&happy&&\n</thinking>\n",
            ["sad"],
        ),
        ("I am happy today.", "I am happy today.", []),
        ("sad\nhappy", "\n", ["sad", "happy"]),
        ("happyhappy", "", ["happy"]),
        ("`happyhappy`\nhappyhappy", "`happyhappy`\n", ["happy"]),
        ("[happy][ref]", "[happy][ref]", []),
        ("[happy]: https://example.com", "[happy]: https://example.com", []),
        ("[nested [happy]](target(x))", "[nested [happy]](target(x))", []),
        (
            "[label\n&&happy&&](target)\n&&sad&&",
            "[label\n&&happy&&](target)\n",
            ["sad"],
        ),
        (
            "[link](target(\n&&happy&&))\n&&sad&&",
            "[link](target(\n&&happy&&))\n",
            ["sad"],
        ),
        ("<!--\n&&happy&&\n--> &&sad&&", "<!--\n&&happy&&\n--> ", ["sad"]),
        (
            "<think><analysis>&&happy&&</analysis>&&sad&&</think>",
            "<think><analysis>&&happy&&</analysis>&&sad&&</think>",
            [],
        ),
        ("f(happy)", "f(happy)", []),
        ("I feel (happy) today", "I feel (happy) today", []),
    ],
)
def test_batch_and_every_chunk_boundary_are_equivalent(text, visible, selected):
    configurations = [[text], list(text)]
    configurations.extend([text[:i], text[i:]] for i in range(len(text) + 1))
    for chunks in configurations:
        parser = MemeParser(
            {"happy", "sad", "自定义"}, alternative=True, loose=True, repeated=True
        )
        actual = "".join(parser.feed(chunk) for chunk in chunks) + parser.finish()
        assert actual == visible, chunks
        assert parser.selections == selected, chunks
        assert parser.finish() == ""
        for token in parser.tokens:
            assert text[token.start : token.end] == token.raw


def test_semantic_markers_share_protection_and_streaming_rules():
    text = "`&&meme:123456789abc&&`\n&&meme:abcdef123456&&\n&&happy&&"
    parser = MemeParser(semantic=True)
    visible = "".join(parser.feed(char) for char in text) + parser.finish()
    assert visible == "`&&meme:123456789abc&&`\n\n"
    assert parser.selections == ["meme:abcdef123456"]


@pytest.mark.parametrize(
    "text",
    [
        "meme:abcdef123456",
        "你好 meme:abcdef123456 正文",
        "MEME:ABCDEF123456\n&&meme:abcdef123456&&",
        "`meme:abcdef123456`\nmeme:123456789abc",
    ],
)
def test_semantic_bare_ids_are_identical_at_every_split(text):
    baseline = MemeParser.parse(text, semantic=True)
    for position in range(len(text) + 1):
        parser = MemeParser(semantic=True)
        visible = (
            parser.feed(text[:position])
            + parser.feed(text[position:])
            + parser.finish()
        )
        assert visible == baseline.text
        assert tuple(parser.tokens) == baseline.tokens


def test_ordinary_words_are_emitted_before_the_line_finishes():
    parser = MemeParser({"happy"})
    assert parser.feed("hello world ") == "hello world "
    assert parser.feed("&") == ""
    assert parser.feed("&happy&&") == ""
    assert parser.finish() == ""


def test_random_chunking_preserves_text_and_marker_offsets():
    randomizer = random.Random(42)
    text = "hello &&happy&&\n```\n&&sad&&\n```\n[happy](https://example.com)\n&&sad&&"
    baseline = MemeParser({"happy", "sad"}, alternative=True)
    expected = baseline.feed(text) + baseline.finish()
    for _ in range(100):
        parser = MemeParser({"happy", "sad"}, alternative=True)
        offset = 0
        output = []
        while offset < len(text):
            end = offset + randomizer.randint(1, 10)
            output.append(parser.feed(text[offset:end]))
            offset = end
        assert "".join(output) + parser.finish() == expected
        assert parser.tokens == baseline.tokens


def test_long_ambiguous_line_is_preserved_and_buffer_is_bounded():
    text = "[" + "x" * (MemeParser.MAX_LINE * 2) + "&&happy&&"
    for chunks in ([text], [text[i : i + 50] for i in range(0, len(text), 50)]):
        parser = MemeParser({"happy"})
        output = []
        for chunk in chunks:
            output.append(parser.feed(chunk))
            assert len(parser._line) <= parser.MAX_LINE
        assert "".join(output) + parser.finish() == text
        assert not parser.selections


def test_resource_limit_keeps_remaining_context_literal():
    text = "```" + "x" * MemeParser.MAX_LINE + "\n&&happy&&\n```\n&&sad&&"
    result = MemeParser.parse(text, {"happy", "sad"})
    assert result.text == text
    assert result.diagnostics == ("line_limit_exceeded",)
    assert not result.tokens
    parser = MemeParser({"happy", "sad"})
    actual = "".join(parser.feed(text[i : i + 99]) for i in range(0, len(text), 99))
    assert actual + parser.finish() == result.text
    assert tuple(parser.diagnostics) == result.diagnostics


def test_chinese_text_streams_without_waiting_for_spaces():
    parser = MemeParser({"happy"})
    assert parser.feed("我觉得") == "我觉得"
    assert parser.feed("这样很好") == "这样很好"


@pytest.mark.parametrize(
    "text, visible",
    [
        ("[image ref 1]: https://example.com/a.png\n正文", "正文"),
        ("file:///tmp/plugin_data/meme_manager/happy/a.png\n正文", "正文"),
        (
            "```\n[image ref 1]: https://example.com/a.png\n```",
            "```\n[image ref 1]: https://example.com/a.png\n```",
        ),
    ],
)
def test_internal_references_are_removed_before_stream_delivery(text, visible):
    parser = MemeParser(strip_references=True)
    actual = "".join(parser.feed(char) for char in text) + parser.finish()
    assert actual == visible


def test_lifecycle_rejects_deltas_after_finish():
    parser = MemeParser()
    parser.finish()
    with pytest.raises(RuntimeError):
        parser.feed("late")


def test_excessive_reasoning_nesting_is_preserved_without_unbounded_stack():
    text = "<think>\n" * 100 + "&&happy&&\n" + "</think>\n" * 100
    parser = MemeParser({"happy"})
    visible = "".join(parser.feed(char) for char in text) + parser.finish()
    assert visible == text
    assert parser.diagnostics == ["context_limit_exceeded"]
    assert len(parser._context.reasoning) == 64


def test_generated_malformed_inputs_preserve_batch_stream_equivalence():
    randomizer = random.Random(123)
    fragments = [
        "happy",
        "sad",
        "&&",
        "[",
        "]",
        "(",
        ")",
        "`",
        "\\",
        " ",
        "\r\n",
        "<think>",
        "</think>",
        "你好",
        "meme:abcdef123456",
    ]
    for _ in range(300):
        text = "".join(randomizer.choice(fragments) for _ in range(20))
        options = {
            "alternative": True,
            "loose": True,
            "repeated": True,
            "semantic": randomizer.choice([True, False]),
        }
        result = MemeParser.parse(text, {"happy", "sad"}, **options)
        parser = MemeParser({"happy", "sad"}, **options)
        actual = "".join(parser.feed(char) for char in text) + parser.finish()
        assert actual == result.text, text
        assert tuple(parser.tokens) == result.tokens, text
        for token in result.tokens:
            assert text[token.start : token.end] == token.raw


def test_nested_brackets_do_not_rescan_each_inner_label(monkeypatch):
    from astrbot_plugin_meme_manager.backend.meme_parser import context

    calls = 0
    original = context.is_escaped

    def counted(text, position):
        nonlocal calls
        calls += 1
        return original(text, position)

    monkeypatch.setattr(context, "is_escaped", counted)
    text = "[" * 500 + "happy" + "]" * 500
    result = MemeParser.parse(text, {"happy"}, alternative=True)
    assert result.text == text
    assert calls < len(text) * 4
