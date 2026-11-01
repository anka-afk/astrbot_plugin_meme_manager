import pytest
from astrbot_plugin_meme_manager.backend.collection_analysis import (
    build_decision_request,
    normalize_decision_answers,
)


@pytest.mark.parametrize(
    "probability,category,content,threshold,rejection,review",
    [
        (0.01, "unknown", "allow", 0.85, "not_meme", False),
        (0.97, "unknown", "reject", 0.85, "content_rule_rejected", False),
        (0.97, "unknown", "allow", 0.85, "", True),
        *[(0.6, "c0", "allow", threshold, "", False) for threshold in (0, 0.2, 0.5)],
        *[
            (0.5, "c0", "allow", threshold, "", True)
            for threshold in (0, 0.2, 0.5, 0.85)
        ],
    ],
)
def test_collection_routes_rejection_uncertainty_and_low_thresholds(
    probability, category, content, threshold, rejection, review
):
    evidence = {
        "caption": "A reaction image",
        "tags": ["reaction"],
        "visible_text": "",
        "chat_use_evidence": "Expressive illustration",
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
    result = normalize_decision_answers(
        answers,
        questions,
        mapping,
        evidence,
        decision_model="test",
        min_meme_confidence=threshold,
        min_category_confidence=0.65,
    )
    assert result["rejection_reason"] == rejection
    assert result["review_required"] is review
    if category == "unknown" and not rejection:
        assert result["category_confidence"] is None
        assert result["probability_source"]["category"] == "no_category"
