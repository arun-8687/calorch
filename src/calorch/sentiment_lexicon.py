"""Local finance-tone sentiment scoring — no LLM, no network, no third-party deps.

The word list at ``data/finance_sentiment_lexicon.json`` is an **original
curation** of finance-tone positive/negative words, inspired by the category
structure of the Loughran-McDonald (LM) financial sentiment word lists
(https://sraf.nd.edu/loughranmcdonald-master-dictionary/) — words like
"strong", "record", "impairment", "litigation" are the kind of terms LM
research established as reliable signal in financial-filing text. This is
*not* the redistributed LM word list itself (which carries its own license
terms); it's a small, hand-picked list built for calorch's guidance/MD&A
snippets, tuned for coverage over precision at this scale.

Scoring is a simple bag-of-words polarity count: for each text, every
lexicon word that appears (whole-word match, case-insensitive) is tallied
as positive or negative, and the text's score is ``(pos - neg) / (pos +
neg)`` — the classic LM-style net-tone ratio, bounded to [-1, 1]. A text
with no lexicon hits scores ``None`` (no signal, not neutral).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


@lru_cache(maxsize=1)
def _lexicon() -> dict[str, frozenset[str]]:
    """Load and cache the packaged lexicon as lowercase word sets."""
    raw = resources.files("calorch.data").joinpath("finance_sentiment_lexicon.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    return {
        "positive": frozenset(w.lower() for w in data.get("positive", [])),
        "negative": frozenset(w.lower() for w in data.get("negative", [])),
    }


def _word_pattern(word: str) -> re.Pattern[str]:
    pattern = _WORD_RE_CACHE.get(word)
    if pattern is None:
        pattern = re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE)
        _WORD_RE_CACHE[word] = pattern
    return pattern


def score_text(text: str) -> float | None:
    """Score a single text on [-1, 1] via lexicon word-boundary matches.

    Returns ``None`` when no lexicon word (positive or negative) is found —
    "no signal", distinct from a neutral 0.0 score.
    """
    if not text:
        return None
    lexicon = _lexicon()
    pos = sum(1 for w in lexicon["positive"] if _word_pattern(w).search(text))
    neg = sum(1 for w in lexicon["negative"] if _word_pattern(w).search(text))
    total = pos + neg
    if total == 0:
        return None
    return (pos - neg) / total


def aggregate(texts: list[str]) -> dict[str, Any]:
    """Aggregate per-text scores into a single sentiment summary.

    Mirrors ``AlphaSenseClient.sentiment``'s label thresholds (±0.1) and
    empty shape (``mean_sentiment: None, sample: 0``) so the two backends
    are interchangeable from the caller's perspective.
    """
    scores = [s for t in texts if (s := score_text(t)) is not None]
    if not scores:
        return {"mean_sentiment": None, "sample": 0}
    mean = sum(scores) / len(scores)
    return {
        "mean_sentiment": round(mean, 3),
        "label": "positive" if mean > 0.1 else "negative" if mean < -0.1 else "neutral",
        "sample": len(scores),
    }
