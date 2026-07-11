"""Free SEC-derived narrative source, built on `edgartools`.

Replaces AlphaSense for the ``narrative`` provider slot (and feeds the
``sentiment`` slot via `calorch.sentiment_lexicon`) with a free source:
guidance/outlook excerpts pulled straight from a company's most recent
SEC filings, rather than a licensed document-intelligence API.

Two filing texts per ticker:
  * the latest 8-K's press-release exhibit (``EX-99.*``) — the earnings
    release, which is where forward guidance usually lives;
  * the latest 10-Q's MD&A (Item 2), falling back to the latest 10-K's
    MD&A (Item 7) when no 10-Q text is available.

`narrative_docs()` normalises these into calorch's narrative-hit shape
(matching `calorch.alphasense._excerpt`, plus a ``snippet`` key built by a
deterministic keyword heuristic — no LLM). `full_texts()` exposes the raw
texts, which `narrative_docs()` and the lexicon-based sentiment provider
both consume, so a given ticker's filings are only fetched once per
client instance (in-process cache, keyed on ticker).

Same lazy-import / injectable `company_factory` pattern as
`calorch.sec_edgartools.EdgarToolsClient`: pass `company_factory` in tests
to avoid the real `edgar` dependency and any network access. When omitted,
`__init__` lazily imports `edgar` and calls `edgar.set_identity(user_agent)`.
Every filing lookup is wrapped defensively — a failure degrades to an
empty result (logged), never raises.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("calorch.sec_narrative")

_GUIDANCE_KEYWORDS: tuple[str, ...] = (
    "guidance", "outlook", "expect", "expects", "anticipate", "forecast", "will be", "fiscal 20",
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_SNIPPET_MAX_LEN = 500
_SNIPPET_MAX_SENTENCES = 4
_FALLBACK_SNIPPET_LEN = 300


def _guidance_snippet(text: str) -> str:
    """Deterministic guidance-focused excerpt — no LLM.

    Splits `text` into sentences, keeps those mentioning a guidance-ish
    keyword, and joins the first few (capped ~500 chars). Falls back to
    the first ~300 chars of the doc when no sentence matches.
    """
    if not text:
        return ""
    stripped = text.strip()
    sentences = _SENTENCE_SPLIT_RE.split(stripped)
    matched = [
        s.strip() for s in sentences
        if s.strip() and any(kw in s.lower() for kw in _GUIDANCE_KEYWORDS)
    ]
    if matched:
        snippet = " ".join(matched[:_SNIPPET_MAX_SENTENCES])
    else:
        snippet = stripped[:_FALLBACK_SNIPPET_LEN]
    return snippet[:_SNIPPET_MAX_LEN].strip()


class SecNarrativeClient:
    """Fetches guidance-relevant SEC filing text via `edgartools`.

    For tests (or any caller avoiding the real dependency/network), pass
    `company_factory` — a callable taking a ticker and returning a
    `Company`-like object exposing `.name` and `.get_filings(form=...)`,
    whose result exposes `.latest(1)` -> an object with `.filing_date`,
    `.accession_no`, `.attachments` (each with `.document_type`, `.text()`)
    and `.obj()` (subscriptable by SEC item label, e.g. `["Item 2"]`).
    """

    def __init__(
        self,
        user_agent: str,
        cache_dir: Path | None = None,
        *,
        company_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._ua = user_agent
        self._cache_dir = cache_dir
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._company_names: dict[str, str] = {}
        if company_factory is not None:
            self._company_factory = company_factory
            return

        import edgar  # lazy import — optional dependency (pip install calorch[edgar])

        edgar.set_identity(user_agent)
        if cache_dir is not None and hasattr(edgar, "set_local_storage_path"):
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                edgar.set_local_storage_path(str(cache_dir))
            except OSError as e:
                log.warning("edgartools cache_dir setup failed, continuing without it: %s", e)
        self._company_factory = edgar.Company

    # ------------------------------------------------------------------
    # Raw filing texts — fetched once per ticker, cached on the instance.
    # ------------------------------------------------------------------
    def full_texts(self, ticker: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """Latest 8-K press release + 10-Q (or 10-K) MD&A, raw text.

        Each item: ``{title, date, type, text}``. `[]` on total failure.
        """
        if ticker not in self._cache:
            self._cache[ticker] = self._fetch_full_texts(ticker)
        return self._cache[ticker][:limit]

    def _fetch_full_texts(self, ticker: str) -> list[dict[str, Any]]:
        try:
            company = self._company_factory(ticker)
        except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
            log.warning("sec_narrative company lookup failed for %s: %s", ticker, e)
            return []
        self._company_names[ticker] = getattr(company, "name", None) or ticker

        texts: list[dict[str, Any]] = []
        press = self._press_release_text(company, ticker)
        if press is not None:
            texts.append(press)
        mda = self._mda_text(company, ticker)
        if mda is not None:
            texts.append(mda)
        return texts

    def _press_release_text(self, company: Any, ticker: str) -> dict[str, Any] | None:
        try:
            filing = company.get_filings(form="8-K").latest(1)
            if filing is None:
                return None
            exhibits = [a for a in filing.attachments if "99" in (getattr(a, "document_type", None) or "")]
            if not exhibits:
                return None
            text = exhibits[0].text()
            if not text or not text.strip():
                return None
            return {
                "title": f"{ticker} 8-K press release",
                "date": str(getattr(filing, "filing_date", "") or ""),
                "type": "8-K",
                "text": text,
                "doc_id": str(getattr(filing, "accession_no", "") or ""),
            }
        except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
            log.warning("sec_narrative 8-K press release fetch failed for %s: %s", ticker, e)
            return None

    def _mda_text(self, company: Any, ticker: str) -> dict[str, Any] | None:
        for form, item_key in (("10-Q", "Item 2"), ("10-K", "Item 7")):
            try:
                filing = company.get_filings(form=form).latest(1)
                if filing is None:
                    continue
                text = str(filing.obj()[item_key])
                if not text or not text.strip():
                    continue
                return {
                    "title": f"{ticker} {form} MD&A",
                    "date": str(getattr(filing, "filing_date", "") or ""),
                    "type": form,
                    "text": text,
                    "doc_id": str(getattr(filing, "accession_no", "") or ""),
                }
            except Exception as e:  # noqa: BLE001 - third-party surface, degrade not raise
                log.warning("sec_narrative %s MD&A fetch failed for %s: %s", form, ticker, e)
                continue
        return None

    # ------------------------------------------------------------------
    # Narrative hits — calorch's `_excerpt` shape, plus a `snippet`.
    # ------------------------------------------------------------------
    def narrative_docs(self, ticker: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """Guidance-focused excerpts, in `calorch.alphasense._excerpt` shape."""
        texts = self.full_texts(ticker, limit=limit)
        company_name = self._company_names.get(ticker, ticker)
        hits = []
        for doc in texts:
            hits.append({
                "title": doc["title"],
                "date": doc["date"],
                "type": doc["type"],
                "company": company_name,
                "ticker": ticker,
                "sentiment": None,
                "source": "sec-filings",
                "doc_id": doc.get("doc_id", ""),
                "snippet": _guidance_snippet(doc["text"]),
            })
        return hits[:limit]
