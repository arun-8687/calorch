"""Optional LLM-based guidance extraction — ingestion-time only.

`calorch.sec_narrative._guidance_snippet` is a deterministic keyword
heuristic: it keeps whichever sentences mention a guidance-ish keyword
("guidance", "outlook", "expect", ...). That works fine for issuers who
give formal numeric guidance, but for issuers like Apple — which discuss
outlook in prose without those trigger words — the heuristic often falls
back to the first few hundred characters of the filing, which is rarely
the most guidance-relevant text.

This module adds an LLM-refined alternative: ask a chat model to quote
the 2-4 sentences that actually discuss guidance/outlook, then run a
verbatim guard over the reply (every claimed quote must exist, near
enough, in the source text) before accepting it. It is a **pure** module
with no framework dependency — `invoke` is a plain ``str -> str``
callable that the caller (currently `IngestionPipeline.ingest_qualitative`)
adapts from whatever chat model `calorch.llm.get_chat_model` returns.

This only runs at ingestion time, writing the refined snippet into the
narrative blob. The live provider path (`calorch.providers.SecNarrativeProvider`)
and demo/mock mode are untouched — they keep the heuristic, so report runs
see zero provider-layer changes; only the blob content differs.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from calorch.config import Settings

_MAX_INPUT_CHARS = 10_000
_DEFAULT_MAX_CHARS = 500
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"\s+")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")
_MIN_MATCH_RATIO = 0.6


def _normalize(s: str) -> str:
    return _WHITESPACE_RE.sub(" ", s).strip()


def _strip_reply(raw: str) -> str:
    text = raw.strip()
    text = _FENCE_RE.sub("", text).strip()
    # Strip a single layer of surrounding quote characters.
    if len(text) >= 2 and text[0] in "\"'" and text[-1] in "\"'":
        text = text[1:-1].strip()
    return text


def llm_guidance_snippet(
    text: str,
    ticker: str,
    invoke: Callable[[str], str],
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str | None:
    """Ask an LLM to quote verbatim the guidance-relevant sentences in `text`.

    Returns `None` — never raises — when the model call fails, declines
    (`NONE`), or its reply doesn't pass the verbatim guard (a defence
    against hallucinated quotes that don't actually appear in the source).
    """
    if not text or not text.strip():
        return None

    source = text[:_MAX_INPUT_CHARS]
    prompt = (
        f"Below is an excerpt from a SEC filing for {ticker}. Quote VERBATIM the "
        "2-4 sentences from the excerpt that contain management guidance, outlook, "
        "or forward-looking statements about revenue, margins, products, or capital "
        "returns. Return only the quoted sentences, separated by spaces, with no "
        "additional commentary, labels, or formatting. If the excerpt contains no "
        "such content, return exactly NONE.\n\n"
        "Only quote text that appears verbatim in the provided excerpt.\n\n"
        f"Excerpt:\n{source}"
    )

    try:
        raw = invoke(prompt)
    except Exception:  # noqa: BLE001 - defensive: any provider failure degrades to None
        return None

    if raw is None:
        return None
    reply = _strip_reply(str(raw))
    if not reply or reply.strip().upper() == "NONE":
        return None

    normalized_source = _normalize(source)
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(reply) if s.strip()]
    if not sentences:
        return None

    matched = sum(1 for s in sentences if _normalize(s) in normalized_source)
    if matched == 0 or matched / len(sentences) < _MIN_MATCH_RATIO:
        return None

    return reply[:max_chars].strip()


def resolve_extractor(settings: Settings) -> str:
    """Resolve `settings.guidance_extractor` ("auto"|"llm"|"heuristic") to a concrete choice.

    Explicit values ("llm", "heuristic") pass through unchanged. "auto"
    resolves to "llm" only when a real chat model is available — i.e.
    `USE_MOCKS` is false and at least one LLM credential (Azure OpenAI or
    Opencode Go) is configured — otherwise "heuristic".
    """
    extractor = settings.guidance_extractor
    if extractor in ("llm", "heuristic"):
        return extractor
    has_llm_credentials = bool(settings.azure_openai_api_key or settings.opencode_go_api_key)
    if not settings.use_mocks and has_llm_credentials:
        return "llm"
    return "heuristic"
