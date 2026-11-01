import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_meme_manager.backend.semantic.query_builder import (
    build_semantic_query,
)


@pytest.mark.asyncio
async def test_direct_reply_query_never_requests_a_model():
    generate = AsyncMock(side_effect=AssertionError("Unexpected generation"))
    query = await build_semantic_query(
        "庆祝\n考试通过", source="reply_text", generate=generate
    )
    assert query == "庆祝 考试通过"
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_query_retains_reply_beyond_generated_phrase_limit():
    reply = "今天发生了很多事情。" * 30 + "最后我还是想安慰你。"
    query = await build_semantic_query(reply, source="reply_text", generate=AsyncMock())
    assert "最后我还是想安慰你" in query
    assert len(query) > 120
    assert len(query) <= 2000


@pytest.mark.asyncio
async def test_generated_query_uses_reference_and_parses_structured_result():
    generate = AsyncMock(
        return_value=SimpleNamespace(completion_text='{"query":"安慰对方"}')
    )
    query = await build_semantic_query(
        "别担心，还有下次机会",
        source="generated",
        reference="参考资料",
        generate=generate,
    )
    assert query == "安慰对方"
    prompt = generate.await_args.args[0]
    assert "别担心" in prompt and "参考资料" in prompt


@pytest.mark.asyncio
async def test_generation_failure_falls_back_but_cancellation_propagates():
    assert (
        await build_semantic_query(
            "祝你好运",
            source="generated",
            generate=AsyncMock(side_effect=RuntimeError("unavailable")),
        )
        == "祝你好运"
    )
    with pytest.raises(asyncio.CancelledError):
        await build_semantic_query(
            "祝你好运",
            source="generated",
            generate=AsyncMock(side_effect=asyncio.CancelledError()),
        )
