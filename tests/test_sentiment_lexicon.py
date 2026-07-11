"""Tests for `calorch.sentiment_lexicon` — pure, no deps, no network."""
from __future__ import annotations

import json
from importlib import resources

from calorch.sentiment_lexicon import aggregate, score_text


# ---------------------------------------------------------------------------
# score_text
# ---------------------------------------------------------------------------
def test_score_text_positive() -> None:
    text = "Revenue growth was strong and margins improved, driving record profitability."
    score = score_text(text)
    assert score is not None
    assert score > 0


def test_score_text_negative() -> None:
    text = "The company faced a significant impairment, ongoing litigation, and a revenue shortfall."
    score = score_text(text)
    assert score is not None
    assert score < 0


def test_score_text_no_hits_returns_none() -> None:
    assert score_text("The cat sat on the mat.") is None


def test_score_text_empty_string_returns_none() -> None:
    assert score_text("") is None


def test_score_text_word_boundary_no_partial_match() -> None:
    # "risk" is a lexicon word; "brisk" must not match it as a substring.
    assert score_text("Deliveries were brisk this quarter.") is None


def test_score_text_case_insensitive() -> None:
    assert score_text("STRONG GROWTH") == score_text("strong growth")


def test_score_text_bounded_range() -> None:
    pos = score_text("strong growth record momentum")
    neg = score_text("decline impairment litigation weakness shortfall headwind")
    assert pos is not None and -1.0 <= pos <= 1.0
    assert neg is not None and -1.0 <= neg <= 1.0


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------
def test_aggregate_empty_texts() -> None:
    result = aggregate([])
    assert result == {"mean_sentiment": None, "sample": 0}


def test_aggregate_all_no_hits() -> None:
    result = aggregate(["nothing to see here", "no signal at all"])
    assert result == {"mean_sentiment": None, "sample": 0}


def test_aggregate_positive_label() -> None:
    result = aggregate(["Strong growth, record revenue, and improved momentum."])
    assert result["label"] == "positive"
    assert result["mean_sentiment"] is not None and result["mean_sentiment"] > 0.1
    assert result["sample"] == 1


def test_aggregate_negative_label() -> None:
    result = aggregate(["Impairment, litigation, and a significant shortfall weighed on results."])
    assert result["label"] == "negative"
    assert result["mean_sentiment"] is not None and result["mean_sentiment"] < -0.1
    assert result["sample"] == 1


def test_aggregate_neutral_label_near_zero() -> None:
    # One positive, one negative word in the same sentence -> score 0.0, "neutral".
    result = aggregate(["Strong results were offset by a shortfall in another segment."])
    assert result["mean_sentiment"] == 0.0
    assert result["label"] == "neutral"


def test_aggregate_mean_of_multiple_texts() -> None:
    result = aggregate([
        "Strong growth and record profitability.",
        "Significant impairment and litigation risk.",
        "no signal here",
    ])
    # Only the two scoreable texts count toward the sample.
    assert result["sample"] == 2
    assert result["mean_sentiment"] is not None


def test_aggregate_rounds_to_three_decimals() -> None:
    result = aggregate(["strong growth decline", "record momentum weakness headwind"])
    if result["mean_sentiment"] is not None:
        assert result["mean_sentiment"] == round(result["mean_sentiment"], 3)


# ---------------------------------------------------------------------------
# Packaged lexicon data
# ---------------------------------------------------------------------------
def test_lexicon_json_loads_from_package_data() -> None:
    raw = resources.files("calorch.data").joinpath("finance_sentiment_lexicon.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert "positive" in data and "negative" in data
    assert len(data["positive"]) > 50
    assert len(data["negative"]) > 50
    assert not (set(w.lower() for w in data["positive"]) & set(w.lower() for w in data["negative"]))
