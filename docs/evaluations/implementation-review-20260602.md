# Calorch Implementation Review
## Date: 2026-06-02
## Status: 45/45 tests passing | Demo runs end-to-end | Real SEC/FRED/H.15 wired

---

## 1. ARCHITECTURE (Strong)

### What Works Well
- **LangGraph StateGraph** is correctly assembled with 8 event types, fan-out via `Send`, `interrupt()` for approval gate, and idempotent delivery with dedup keys
- **Separation of concerns**: scan → classify → enrich → render → deliver → aggregate is clean
- **Context pattern**: module-level `Context` dataclass cleanly injects runtime dependencies without polluting LangGraph state
- **Protocol-based provider layer**: `PriceProvider`, `ConsensusProvider`, `MacroProvider`, `SegmentProvider`, `NarrativeProvider` — swap-to-paid-vendor is config-only
- **Two-pass classifier**: Pass 1 (deterministic keywords) + Pass 2 (LLM Structured Output) with SEC fast-path is pragmatic
- **SEC form classification**: `_8K_ITEMS_MAP` correctly maps Item 2.02 → earnings_call, Item 7.01 → channel_check, etc.
- **Enterprise data client abstraction**: `EnterpriseDataClient.fetch()` returns uniform snapshots regardless of underlying vendor
- **MemorySaver checkpointer** enables resume after `interrupt()`
- **Azure Container Apps deployment** files are present (Dockerfile, deploy scripts, cost analysis)

### Architecture Weaknesses
- **No true multi-tenant isolation**: single global `_CTX` means one process per tenant
- **Event dedup is per-run only**: no cross-run deduplication of the same filing
- **Weekly briefing is too thin**: just an HTML table of counts; prep_scanner produces multi-page DOCX with cross-event analysis
- **No retry/backoff for SEC rate limits beyond a simple 1/9s timer**

---

## 2. DATA SOURCES (Mixed — Free sources work, paid stubs only)

### Live & Working
| Source | Status | Coverage | Quality |
|--------|--------|----------|---------|
| **FOMC H.15** | Live, key-less | 11 treasury series (1M→30Y) + EFFR | Excellent — 2026-05-29 data confirmed |
| **FRED** | Live, key optional | VIX, S&P 500, oil, gold, BTC, CPI, unemployment, FX | Good (no-key calls 400 on some series) |
| **SEC EDGAR submissions** | Live | 10-K/10-Q/8-K filings + metadata | Good — 1,034 filings processed in 14-day demo |
| **SEC iXBRL parser** | Live, **buggy** | Product/geo segment revenue from 10-K/10-Q | **Parses but values wrong** (see §3) |
| **SEC EFTS** | Live, **buggy** | Guidance/outlook text search | **Finds filings but snippets empty** (see §3) |

### Stub Only (No Real Data)
| Source | Stub Returns | Gap |
|--------|------------|-----|
| **Price** | Placeholders (None for all fields) | No real-time quote, market cap, 52w range, YTD % |
| **Consensus** | Placeholders (None for all fields) | No EPS estimates, revenue estimates, analyst counts, price targets |
| **Segments** | Curated AAPL/MSFT/GOOGL/AMZN splits | Only works for those 4 tickers; all others empty |
| **Narrative** | 2 canned AAPL excerpts | No real guidance text extraction |

### Not Yet Integrated
| Source | Why Missing | Impact |
|--------|-------------|--------|
| **Tiingo** ($50/mo) | Not approved | Would give real prices + fundamentals for quote box |
| **Refinitiv/FactSet/Bloomberg** | Not picked | Would give consensus, segments, transcripts for all tickers |
| **Companyfacts API** | Not wired | Could give consolidated financials without iXBRL parsing |

---

## 3. CRITICAL BUGS & ISSUES

### Bug A: iXBRL Segment Values Are Wrong
**Severity: HIGH**

The `_segment_table_rows()` renderer divides `val / 1e9` expecting raw dollars. But iXBRL values from AAPL's 10-Q are in **millions** ("80,208" = $80,208M = $80.2B). Result: all segment revenues show as **$0.00B**.

**Root cause**: No unit scale detection in iXBRL parser. The `decimals` attribute on iXBRL facts indicates scale but is not read.

**Fix needed**: Either (a) detect `decimals="-6"` and multiply by 1e6, or (b) hardcode "SEC iXBRL values are in millions" and multiply by 1e6 in renderer.

### Bug B: EFTS Snippets Are Empty
**Severity: MEDIUM**

EFTS search finds filings (e.g., "expects total revenue" returns 2 AAPL 10-Qs) but the `snippet` field is always empty string.

**Root cause**: EFTS response no longer includes highlighted text in `_source` by default. The `highlight` field is empty `{}`. Need to fetch the actual filing and extract context, or use a different EFTS parameter.

**Fix needed**: After EFTS returns a hit, fetch the actual filing text via the primary document URL and search for the query term locally to build a snippet.

### Bug C: EFTS Multi-Form Filter Broken
**Severity: MEDIUM**

EFTS returns 0 hits when multiple forms are joined with `-` (e.g. `forms=10-Q-8-K-10-K`). The API only accepts one form per request.

**Status**: Partially fixed — now issues one request per form and merges. But snippets still empty.

### Bug D: FRED No-Key Calls Fail on Many Series
**Severity: LOW**

Without `FRED_API_KEY`, calls to `SP500`, `DEXUSEU`, `CPIAUCSL`, `UNRATE` return HTTP 400. VIX, treasury rates, oil, gold, BTC work.

**Workaround**: `FreeMacroProvider` falls back to H.15 for treasury rates. Other series (S&P 500, CPI, unemployment) show "—".

### Bug E: Stub Segment Provider Returns Old Data
**Severity: LOW**

Stub iXBRL returns Q1 FY2026 AAPL data (period_end 2025-12-27) even when the live parser found Q2 FY2026 data (period_end 2026-03-28). The stub is hardcoded and not refreshed.

---

## 4. REPORT QUALITY vs. prep_scanner

### What prep_scanner AAPL Q2 FY2026 Pack Contains (4,397 words)
1. **Executive Snapshot** — Market cap, price, 52w range, consensus rating, price target
2. **Last Quarter Performance** — EPS actual/estimate/surprise, revenue actual/estimate/surprise
3. **Q2 FY2026 Consensus Estimates** — EPS/rev estimates, range, # analysts, YoY growth
4. **Key Financial Metrics** — Gross margin, operating margin, net margin, ROE, ROA, ROIC
5. **Valuation Multiples** — P/E TTM, Forward P/E, EV/EBITDA, P/S, P/B, PEG
6. **Balance Sheet Highlights** — Cash, debt, net debt, D/E, current ratio, FCF
7. **Revenue Segmentation** — Product splits with % of revenue, geographic splits with %
8. **Analyst Sentiment & Fund Activity** — Buy/hold/sell counts, price target distribution, insider activity, institutional trends
9. **Key Questions & Themes** — 6 themes (iPhone demand, Services growth, AI, Gross margins, Capital returns, China, Vision Pro)
10. **ESG Snapshot** — Risk score, environmental, social, governance
11. **Recent Price Performance** — 1W/1M/3M/YTD/1Y returns vs S&P 500

### What calorch AAPL Report Contains (~350 words)
1. **Event metadata** — Type, date, organizer, SEC form info
2. **10-section structure** — But 8 of 10 sections say "(not available for this filing)"
3. **Financials table** — Ticker, company, revenue, net income, EPS, form (from enterprise data mock)
4. **Macro box** — 11 treasury series from FOMC H.15 (live, good)
5. **Segment table** — Shows labels but **$0.00B** values (bug)
6. **Geography table** — Shows labels but **$0.00B** values (bug)
7. **Guidance excerpts** — Empty (EFTS bug)
8. **Footer** — Confidence badge, generation timestamp

### Gap Analysis
| Feature | prep_scanner | calorch | Gap Size |
|---------|-------------|---------|----------|
| **Real-time price** | $248.80 | None | Critical |
| **Market cap** | $3,656.84B | None | Critical |
| **52-week range** | $169.21-$260.10 | None | Critical |
| **Consensus EPS** | $1.73 (avg), $1.54-$1.87 (range) | None | Critical |
| **Consensus revenue** | $101.98B (avg), $93.67B-$108.50B | None | Critical |
| **# Analysts** | 28 | None | Critical |
| **YoY growth** | Est. +8.1% | None | Critical |
| **Profitability margins** | Gross 46.9%, Op 33.9%, Net 26.7% | None | Critical |
| **Valuation multiples** | P/E 37.4x, Forward 31.2x, EV/EBITDA 28.5x | None | Critical |
| **Balance sheet** | Cash $30.3B, Debt $96.8B, FCF $109.2B | None | Critical |
| **Segment splits** | iPhone 48.1%, Services 18.3%, etc. | Labels only, $0 values | High |
| **Geographic splits** | Americas 42%, Europe 27%, China 17% | Labels only, $0 values | High |
| **Analyst sentiment** | 68 Buy / 33 Hold / 7 Sell | None | Critical |
| **Price target** | $316.36 (+27.2% upside) | None | Critical |
| **Recent price perf** | +1.2% 1W, -3.5% 1M, +5.8% 3M, +6.3% YTD | None | Critical |
| **ESG** | Low risk, carbon neutral, privacy | None | Medium |
| **Key questions** | 7 themes with 2-3 sub-questions each | None | Critical |
| **Macro context** | None | 11 treasury series | calorch wins here |
| **SEC filing link** | None | EDGAR link | calorch wins here |

---

## 5. RENDERER ISSUES

### The 10-Section Structure Is Mostly Placeholders
The `_render_earnings_call_sections()` maps builder headings to numbered titles. When no builder section matches, it prints "(not available for this filing)". Currently:
- **Only 2 of 10 sections have real content**: "1. Headline" and "10. Q&A Highlights"
- **8 sections are empty**: Key Financials, Guidance, Segment Performance, Margin Walk, Cash Flow, Capex/R&D, Buyback/Dividend, Risk Factors

**Root cause**: `build_analysis()` only creates 4 builder sections (Filing summary, Forward commentary, Variance vs. prior, Action items). None of these map to sections 2-9.

### The `_base()` Function Doesn't Use Enterprise Data
```python
def _base(title, ev, cls, ed) -> EventAnalysis:
    return EventAnalysis(
        event_id=ev.id,
        event_type=cls.final_label,
        title=title,
        confidence=cls.confidence,
        tickers=_tickers_from_subject(ev.subject) or list(ed.get("snapshots", {}).keys())[:3],
        source_attribution=f"Source: {ed.get('source', 'mock')} @ {ed.get('as_of', '')}",
    )
```
The enterprise data `ed` is only used for the `snapshots` table. All the rich fields (guidance, transcript excerpts, actual financials) are **not extracted** into builder sections.

### Missing Renderer Features
- No chart/visualization support (prep_scanner likely has none either, but no placeholder)
- No multi-period comparison tables (Q1 FY26 vs Q1 FY25 vs Q4 FY25)
- No variance analysis (actual vs consensus)
- No color coding for beats/misses

---

## 6. TEST COVERAGE

### Current Tests (45 passing)
| File | Tests | What They Cover |
|------|-------|----------------|
| `test_graph.py` | 6 | Graph compilation, end-to-end 8 events, approval gate, idempotency, empty calendar |
| `test_classifier.py` | ~5 | Keyword scoring, LLM classification, SEC form fast-path |
| `test_renderers.py` | 4 | DOCX generation, 10-section structure, HTML email, role inference |
| `test_tools.py` | ~5 | Graph client, OneDrive, repository, enterprise data client |
| `test_fred.py` | 13 | Stub FRED, stub H.15, H.15 parsing, FRED caching, FreeMacroProvider |
| `test_sec_providers.py` | 10 | iXBRL parsing, EFTS search, StubIxbrlClient, StubEftsClient |
| `test_providers.py` | 9 | ProviderBundle, build_providers config gating, dispatcher |
| `test_serve.py` | ~3 | FastAPI health, run endpoint, runs/{id} |

### What's NOT Tested
- **Real SEC iXBRL parsing with actual values** (only tested that it parses, not that values are correct)
- **Real EFTS snippet extraction** (snippets are empty in tests too)
- **Real FRED with API key** (tests only test caching, not live data)
- **DOCX content validation** (tests only check file exists and >5KB)
- **Event classification accuracy** (no golden set)
- **Approval gate UI/interrupt behavior** (only tested programmatically)
- **Error handling for network failures** (SEC down, FRED down, etc.)
- **Cross-run deduplication**
- **OneDrive upload/download**
- **Azure OpenAI integration** (mock only)

---

## 7. CODE QUALITY ISSUES

### Minor Issues
1. **Inconsistent naming**: `build_providers` vs `make_providers` in tools.py
2. **Magic numbers**: `1e9` for billions hardcoded in renderer without documentation
3. **Type annotations**: Some `Any` types where Protocols could be used
4. **Error suppression**: Too many `except Exception: pass` patterns hide real failures
5. **No structured logging**: Uses basic `logging` instead of structured JSON logging
6. **No metrics/observability**: No Prometheus counters, no OpenTelemetry spans

### Medium Issues
1. **iXBRL namespace handling**: The `XBRL_NS` and `XBRLI_NS` are identical strings — confusing
2. **Context mutable global**: `_CTX` is mutable global state; threading issues possible
3. **No pagination for SEC submissions**: If a company has >1000 filings, the submissions API truncates
4. **Cache invalidation**: FRED/H.15 caches have TTL but no manual invalidation mechanism
5. **No config validation**: `Settings` dataclass accepts any string for enum-like fields

---

## 8. WHAT WOULD IT TAKE TO MATCH prep_scanner?

### Tier 1: Fix Critical Bugs (1-2 days)
1. **Fix iXBRL value scaling** — multiply by 1e6 (or detect `decimals` attribute)
2. **Fix EFTS snippet extraction** — fetch filing text and search locally
3. **Populate the 10-section structure** — extract guidance, margins, cash flow, buyback from enterprise data

### Tier 2: Add Real Price/Consensus Data (2-3 days)
1. **Integrate Tiingo** ($50/mo) — real prices, market cap, 52w range, YTD %
2. **Add quote box to renderer** — price, change, market cap, 52w range
3. **Add consensus table** — EPS/rev estimates, surprise, # analysts, price target

### Tier 3: Rich Financial Analysis (3-5 days)
1. **Add profitability table** — gross/op/net margins, ROE, ROA, ROIC
2. **Add valuation table** — P/E, Forward P/E, EV/EBITDA, P/S, P/B, PEG
3. **Add balance sheet table** — cash, debt, net debt, D/E, current ratio, FCF
4. **Add YoY/QoQ comparison** — actual vs prior period vs consensus
5. **Add segment/geo pie charts** or at least % of total

### Tier 4: Investment Research Features (5-7 days)
1. **Analyst sentiment module** — buy/hold/sell distribution, price target range
2. **Recent price performance** — 1W/1M/3M/YTD/1Y vs S&P 500
3. **ESG snapshot** — risk score, environmental, social, governance
4. **Key questions generator** — thematic questions based on event type + ticker
5. **Insider activity** — net buying/selling from Form 4 filings
6. **Institutional trends** — 13F filing analysis

### Tier 5: Channel Check & KOL Deep Dives (3-4 days)
1. **Model assumptions table** — what to validate, current assumption, confidence
2. **Standardized questionnaire** — 15-20 questions per event type
3. **Comparison framework** — model vs channel findings vs variance
4. **Historical log** — track findings across multiple checks

---

## 9. SUMMARY

### Strengths
- Solid LangGraph architecture with proper fan-out, interrupts, and idempotency
- Clean provider protocol layer enables vendor swapping
- Real free sources (FOMC H.15, FRED, SEC EDGAR) are wired and working
- Good test coverage for the core pipeline (45/45 passing)
- Azure Container Apps deployment ready

### Weaknesses
- **Report quality is far below prep_scanner** — mostly placeholders
- **iXBRL values are wrong** ($0.00B instead of actual billions)
- **EFTS snippets are empty** — no real guidance text extraction
- **No real price or consensus data** — stub only
- **10-section structure is 80% empty** — only headline and action items populated
- **Missing key research features**: valuation multiples, balance sheet, analyst sentiment, price performance, ESG

### Recommendation
The architecture is production-ready. The data pipeline needs:
1. **Bug fixes** (iXBRL scaling, EFTS snippets) — 1-2 days
2. **Tiingo integration** ($50/mo) for real prices — 1 day
3. **Renderer enrichment** to populate all 10 sections with real data — 3-5 days
4. **Terminal vendor** (Refinitiv/FactSet/Bloomberg) for consensus and segments — 1-2 weeks

With these, calorch would produce reports **comparable to prep_scanner** in depth and quality.
