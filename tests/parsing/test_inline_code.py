import random

import pytest
from astrbot_plugin_meme_manager.backend.meme_parser import MemeParser


@pytest.mark.parametrize(
    "semantic, marker, selected",
    [
        (False, "&&happy&&", "happy"),
        (True, "&&meme:3376c76aadfd&&", "meme:3376c76aadfd"),
    ],
)
@pytest.mark.parametrize("separator", [" ", "\n", "\r\n"])
def test_kaomoji_backtick_does_not_hide_markers(semantic, marker, selected, separator):
    prefix = "好的喵～ (´･ω･`)"
    text = prefix + separator + marker
    expected = prefix + separator
    for chunks in (
        [text],
        list(text),
        *([text[:i], text[i:]] for i in range(len(text) + 1)),
    ):
        parser = MemeParser({"happy"}, semantic=semantic)
        visible = "".join(parser.feed(chunk) for chunk in chunks) + parser.finish()
        assert visible == expected
        assert parser.selections == [selected]
        assert parser.finish() == ""
        assert len(parser.tokens) == 1
        token = parser.tokens[0]
        assert text[token.start : token.end] == marker


@pytest.mark.parametrize(
    "text, visible, selected",
    [
        ("text ` &&happy&&\n&&sad&&", "text ` \n", ["happy", "sad"]),
        ("text `&&happy&&", "text `", ["happy"]),
        ("text `a &&happy&&``", "text `a ``", ["happy"]),
        ("text ``a &&happy&&`", "text ``a `", ["happy"]),
        ("text `a ``&&happy&&``\n&&sad&&", "text `a ``&&happy&&``\n", ["sad"]),
        ("text `&&happy&&` then `&&sad&&", "text `&&happy&&` then `", ["sad"]),
        ("text `a\n&&happy&&\nb` &&sad&&", "text `a\n&&happy&&\nb` ", ["sad"]),
        ("text ``a `\n&&happy&&\nb`` &&sad&&", "text ``a `\n&&happy&&\nb`` ", ["sad"]),
        (
            "text `\n<think>\n&&sad&&\n</think>\n&&happy&&",
            "text `\n<think>\n&&sad&&\n</think>\n",
            ["happy"],
        ),
        (
            "text `\n<!--\n&&sad&&\n-->\n&&happy&&",
            "text `\n<!--\n&&sad&&\n-->\n",
            ["happy"],
        ),
        (
            "text `\n[label\n&&sad&&](target)\n&&happy&&",
            "text `\n[label\n&&sad&&](target)\n",
            ["happy"],
        ),
        (
            "text `\n```python\n&&sad&&\n```\n&&happy&&",
            "text `\n```python\n&&sad&&\n```\n",
            ["happy"],
        ),
        (
            "text `\n```js\nconst s = `hi`;\n```\n&&happy&&",
            "text `\n```js\nconst s = `hi`;\n```\n",
            ["happy"],
        ),
        (
            "text `\n```\n`\n```\n&&happy&&",
            "text `\n```\n`\n```\n",
            ["happy"],
        ),
        ("```python\n&&happy&&", "```python\n&&happy&&", []),
        ("text \\` &&happy&&", "text \\` ", ["happy"]),
    ],
)
def test_inline_resolution_preserves_protected_regions_at_every_split(
    text, visible, selected
):
    configurations = [[text], list(text)]
    configurations.extend([text[:i], text[i:]] for i in range(len(text) + 1))
    for chunks in configurations:
        parser = MemeParser({"happy", "sad"})
        output = "".join(parser.feed(chunk) for chunk in chunks) + parser.finish()
        assert output == visible, chunks
        assert parser.selections == selected, chunks
        assert all(
            text[token.start : token.end] == token.raw for token in parser.tokens
        )


def test_stream_holds_unconfirmed_markers_until_inline_code_is_resolved():
    parser = MemeParser({"happy", "sad"})
    assert parser.feed("hello ") == "hello "
    assert parser.feed("`example\n&&happy&&\n") == ""
    assert parser.selections == []
    assert parser.feed("end`\n&&sad&&\n") == "`example\n&&happy&&\nend`\n\n"
    assert parser.selections == ["sad"]
    assert parser.finish() == ""


def test_unmatched_replay_preserves_reference_removal_and_source_offsets():
    text = "text `\n[image ref 1]: https://example.com/a.png\n&&happy&&"
    for chunks in ([text], list(text)):
        parser = MemeParser({"happy"}, strip_references=True)
        output = "".join(parser.feed(chunk) for chunk in chunks) + parser.finish()
        assert output == "text `\n"
        assert parser.selections == ["happy"]
        assert [token.kind for token in parser.tokens] == ["reference", "strict"]
        assert all(
            text[token.start : token.end] == token.raw for token in parser.tokens
        )


@pytest.mark.parametrize("filter_all", [False, True])
def test_pending_inline_buffer_is_bounded_without_leaking_filtered_markers(filter_all):
    text = "hello `\n" + "x\n" * MemeParser.MAX_LINE + "&&happy&&\n"
    expected = text.replace("&&happy&&", "") if filter_all else text
    baseline = None
    for size in (1, 47, len(text)):
        parser = MemeParser({"happy"}, filter_all=filter_all)
        output = []
        for offset in range(0, len(text), size):
            output.append(parser.feed(text[offset : offset + size]))
            assert parser._pending_size + len(parser._line) <= parser.MAX_LINE
        output.append(parser.finish())
        assert "".join(output) == expected
        assert parser.selections == []
        assert parser.diagnostics == ["inline_code_limit_exceeded"]
        if baseline is None:
            baseline = parser.tokens
        assert parser.tokens == baseline


def test_streaming_filter_all_matches_batch_across_chunk_boundaries():
    for text in ("foo:bar:\n", "ameme:abcdef123456\n", "user:id: 123\n"):
        batch = MemeParser.parse(text, {"happy"}, filter_all=True).text
        for split in range(1, len(text)):
            parser = MemeParser({"happy"}, filter_all=True)
            streamed = (
                parser.feed(text[:split]) + parser.feed(text[split:]) + parser.finish()
            )
            assert streamed == batch, (text, split)


def test_replayed_unmatched_code_keeps_reasoning_resource_guard():
    text = "text `\n" + "<think>\n" * 100 + "&&happy&&\n" + "</think>\n" * 100
    for chunks in ([text], list(text)):
        parser = MemeParser({"happy"})
        output = "".join(parser.feed(chunk) for chunk in chunks) + parser.finish()
        assert output == text
        assert parser.selections == []
        assert parser.diagnostics == ["context_limit_exceeded"]
        assert len(parser._context.reasoning) == parser._context.MAX_DEPTH


def test_random_chunking_with_unmatched_and_matched_runs_is_equivalent():
    randomizer = random.Random(154)
    fragments = [
        "`",
        "``",
        " ",
        "\n",
        "\r\n",
        "&&happy&&",
        "&&sad&&",
        "hello",
        "<think>",
        "</think>",
    ]
    for _ in range(200):
        text = "".join(randomizer.choice(fragments) for _ in range(20))
        baseline = MemeParser.parse(text, {"happy", "sad"})
        parser = MemeParser({"happy", "sad"})
        visible = []
        offset = 0
        while offset < len(text):
            end = offset + randomizer.randint(1, 9)
            visible.append(parser.feed(text[offset:end]))
            offset = end
        visible.append(parser.finish())
        assert "".join(visible) == baseline.text
        assert tuple(parser.tokens) == baseline.tokens
        assert tuple(parser.diagnostics) == baseline.diagnostics


@pytest.mark.parametrize("ticks", ["``", "```", "````"])
@pytest.mark.parametrize("separator", [" ", "\n"])
def test_escaped_run_tail_without_a_matching_raw_closer_is_literal(ticks, separator):
    prefix = "hello " + chr(92) + ticks
    text = prefix + separator + "&&happy&&"
    for chunks in (
        [text],
        list(text),
        *([text[:i], text[i:]] for i in range(len(text) + 1)),
    ):
        parser = MemeParser({"happy"})
        output = "".join(parser.feed(chunk) for chunk in chunks) + parser.finish()
        assert output == prefix + separator
        assert parser.selections == ["happy"]
        assert parser._context.ticks == 0
        assert all(
            text[token.start : token.end] == token.raw for token in parser.tokens
        )


@pytest.mark.parametrize(
    "text, selected",
    [
        ("text `code " + chr(92) + "`` &&happy&&", ["happy"]),
        ("text " + chr(92) + "`` &&happy&& `", []),
        ("text ``code " + chr(92) + "`` &&happy&&", ["happy"]),
        ("text " + chr(92) + "``` &&happy&& ``", []),
    ],
)
def test_escaped_tails_can_open_but_only_raw_runs_can_close_code(text, selected):
    for chunks in ([text], list(text)):
        parser = MemeParser({"happy"})
        output = "".join(parser.feed(chunk) for chunk in chunks) + parser.finish()
        assert parser.selections == selected
        assert output == (text.replace("&&happy&&", "") if selected else text)
        assert parser._context.ticks == 0


@pytest.mark.parametrize(
    "text",
    [
        "[`hello] &&happy&& `",
        "[`hello]\n&&happy&&\nend`",
        "display [`code`]",
        "display [`&&happy&&`]",
        "display [outer [inner `code`]]",
    ],
)
@pytest.mark.parametrize("alternative", [False, True])
def test_bracket_contents_keep_matching_code_protection(text, alternative):
    for chunks in (
        [text],
        list(text),
        *([text[:i], text[i:]] for i in range(len(text) + 1)),
    ):
        parser = MemeParser({"happy"}, alternative=alternative, remove_invalid=True)
        output = "".join(parser.feed(chunk) for chunk in chunks) + parser.finish()
        assert output == text
        assert parser.selections == []
        assert parser.tokens == []


def test_nested_brackets_with_code_do_not_rescan_every_inner_label(monkeypatch):
    from astrbot_plugin_meme_manager.backend.meme_parser import context

    calls = 0
    original = context.is_escaped

    def counted(text, position):
        nonlocal calls
        calls += 1
        return original(text, position)

    monkeypatch.setattr(context, "is_escaped", counted)
    text = "[" * 500 + "`&&happy&&`" + "]" * 500
    result = MemeParser.parse(text, {"happy"}, alternative=True, remove_invalid=True)
    assert result.text == text
    assert result.selections == ()
    assert calls < len(text) * 4


def test_escaped_tail_replay_does_not_rescan_all_positions_for_each_line(monkeypatch):
    from astrbot_plugin_meme_manager.backend.meme_parser import parser as parser_module

    iterations = 0

    class CountedSet(set):
        def __iter__(self):
            nonlocal iterations
            for item in super().__iter__():
                iterations += 1
                yield item

    monkeypatch.setattr(parser_module, "set", CountedSet, raising=False)
    text = (chr(92) + "``\n") * 2000 + "&&happy&&"
    result = MemeParser.parse(text, {"happy"})
    assert result.text == text.replace("&&happy&&", "")
    assert result.selections == ("happy",)
    assert result.diagnostics == ()
    assert iterations < len(text) * 4
