from types import SimpleNamespace

import pytest
from astrbot_plugin_meme_manager.backend.auto_collect import AutoCollectManager
from astrbot_plugin_meme_manager.backend.collection_analysis import (
    build_decision_request,
    normalize_decision_answers,
)


def collection_case(
    *, probability=0.97, category="c0", content="allow", threshold=0.85
):
    evidence = {
        "caption": "A reaction image",
        "tags": ["reaction"],
        "visible_text": "",
        "chat_use_evidence": "Expressive reaction illustration",
    }
    _, questions, mapping = build_decision_request(
        evidence,
        {"happy": "Celebration"},
        content_review_enabled=True,
        content_review_prompt="Only collect suitable chat reactions.",
    )
    answers = {
        "meme_usage": {"type": "noul", "noul": probability},
        "category": {
            "type": "choice",
            "choice": category,
            "probabilities": {"c0": 0.9, "unknown": 0.1},
        },
        "content_rule": {"type": "choice", "choice": content},
    }
    return normalize_decision_answers(
        answers,
        questions,
        mapping,
        evidence,
        decision_model="test",
        min_meme_confidence=threshold,
        min_category_confidence=0.65,
    )


def test_conclusive_non_meme_is_rejected_even_when_no_category_fits():
    result = collection_case(probability=0.01, category="unknown")
    assert result["rejection_reason"] == "not_meme"
    assert result["review_required"] is False


def test_explicit_content_rejection_takes_priority_over_unknown_category():
    result = collection_case(category="unknown", content="reject")
    assert result["rejection_reason"] == "content_rule_rejected"
    assert result["review_required"] is False


def test_unknown_choice_is_not_reported_as_high_confidence_category():
    result = collection_case(category="unknown")
    assert result["review_required"]
    assert result["category_confidence"] is None
    assert result["probability_source"]["category"] == "no_category"


@pytest.mark.parametrize("threshold", [0, 0.2, 0.5])
def test_low_configured_threshold_does_not_invert_positive_and_negative(threshold):
    result = collection_case(probability=0.6, threshold=threshold)
    assert result["rejection_reason"] == ""
    assert result["review_required"] is False


@pytest.mark.parametrize("threshold", [0, 0.2, 0.5, 0.85])
def test_even_probability_is_always_uncertain(threshold):
    result = collection_case(probability=0.5, threshold=threshold)
    assert result["review_required"] is True
    assert result["rejection_reason"] == ""


def test_decision_cache_ignores_observation_time_but_tracks_threshold_changes():
    manager = object.__new__(AutoCollectManager)
    manager.plugin = SimpleNamespace(
        decision_client=SimpleNamespace(
            base_url="https://example.test/v1",
            model="jev",
            timeout=15,
            max_concurrency=2,
        )
    )
    manager.manual_review = False
    manager.content_review_enabled = True
    manager.content_review_prompt = "rule"
    manager.min_meme_confidence = 0.85
    manager.min_category_confidence = 0.65
    evidence = {"caption": "reaction", "tags": ["happy"]}
    original = manager._decision_cache_key("pack", {"happy": "Happy"}, evidence)
    cached = manager._decision_cache_key(
        "pack", {"happy": "Happy"}, {**evidence, "checked_at": "2026-10-31T00:00:00Z"}
    )
    assert cached == original
    manager.min_meme_confidence = 0.99
    assert manager._decision_cache_key("pack", {"happy": "Happy"}, evidence) != original
