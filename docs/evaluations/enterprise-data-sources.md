# Revised data-source proposal — Enterprise-grade

> **Context:** calorch is an enterprise research-orchestration system. Data
> sources must have **commercial terms of service that permit enterprise
> redistribution**, **uptime SLAs**, and **documented APIs**. No "unofficial"
> or "reverse-engineered" sources, even if they currently work.
>
> This document replaces the earlier "free / unofficial" proposal.
>
> **Status (as of 2026-06-02):** Phases 1, 3, 5, 6 are **shipped** in the
> calorch codebase. Phase 2 (Tiingo) and Phase 4 (terminal vendor) are
> stubbed and gated by env vars — the provider dispatcher in
> `src/calorch/providers.py` selects them automatically.

---

## Hard exclusion list

The following sources are **banned** for the enterprise build, regardless of
how well they work today:

| Source | Reason for exclusion |
|---|---|
| `yfinance` Python library | Reverse-engineers Yahoo's auth-gated endpoints. No contract, no SLA, IP rate-limited, breaks without notice. Some firm IT policies explicitly block reverse-engineering. |
| Yahoo Finance v7/v10 `quoteSummary` | Requires HTTP cookie auth; intended for personal browser use, not redistribution. |
| Yahoo Finance v8 chart (unauthenticated) | Not contractually permitted for redistribution; documented ToS forbids scraping. Common in side projects; not enterprise. |
| `stooq.com` scraping | No commercial API; no contract; no SLA; data quality varies. |
| Alpha Vantage free tier (25/day) | ToS allows personal/non-commercial use; enterprise tier exists but unverified pricing for calorch's use case. |
| Any "free API" directory listing with a public-apis.io/aggregator URL | Curated for hobbyists, not vetted for enterprise ToS. |
| Scraping Benzinga, TipRanks, MarketBeat, WallStreetZen | None of these permit scraping in their ToS; all require paid commercial license for redistribution. |

---

## Authoritative, enterprise-grade sources

### Tier 1 — Already contracted (assumed baseline)

The Reference Plan Document (original ADR) assumes the firm already has
M365 E3 + Bloomberg or Refinitiv + FactSet or S&P Capital IQ + (optionally)
MSCI/Sustainalytics. If calorch runs inside a research team, **all of these
are already licensed** — the question is how to access them programmatically.

| Vendor | What it covers | Access method for calorch | Status |
|---|---|---|---|
| **Microsoft Graph** | Calendar, mail, OneDrive, Teams | OAuth client credentials; `azure-identity` SDK | ✅ shipped (`src/calorch/tools.py`) |
| **Refinitiv (LSEG) Data Platform** | Fundamentals, I/B/E/S consensus, price targets, ESG, segments | RDP Libraries for Python (`.DP.Library`) | 🔌 stub (`src/calorch/providers.py:StubConsensusProvider`) — wire in when key issued |
| **FactSet** (alternative) | Fundamentals, FactSet Estimates, FactSet Fundamentals, Transcripts | `fdsdk` Python; or Workstation API | 🔌 stub — same role as Refinitiv |
| **Bloomberg** (alternative) | BQL + `blpapi` | Enterprise terminal seat + BQL access | 🔌 stub — same role |
| **S&P Capital IQ** (alternative) | Fundamentals + estimates + segments | `CapIQ` Python SDK | 🔌 stub — same role |
| **MSCI / Sustainalytics ESG** | ESG scores | Custom data file delivery; or via RDP/Refinitiv ESG add-on | 🔌 stub |
| **EDGAR** (SEC) | Filings, XBRL, iXBRL segments, EFTS | Official SEC EDGAR REST API, free, supported, ToS-compliant | ✅ shipped (`src/calorch/sec.py`, `sec_ixbrl.py`, `sec_efts.py`) |

### Tier 2 — Free, official sources (also shipped)

| Source | What it covers | Status |
|---|---|---|
| **FRED API** (St. Louis Fed) | Macro: VIX, 10Y, oil, gold, BTC, CPI, GDP, unemployment, Fed Funds | ✅ shipped (`src/calorch/fred.py`); optional key |
| **FOMC H.15** (Federal Reserve) | Treasury yield curve + EFFR, daily, no key | ✅ shipped (`src/calorch/fed_h15.py`) |
| **SEC iXBRL** | Segment & geographic revenue, parsed from inline XBRL instance docs | ✅ shipped (`src/calorch/sec_ixbrl.py`) |
| **SEC EFTS** | Full-text search across all filings (guidance/outlook snippets) | ✅ shipped (`src/calorch/sec_efts.py`) |

### Tier 3 — Recommended paid additions (cheap)

These fill gaps that the above either cover expensively (per-seat
Refinitiv = $22k+/yr) or don't cover at all:

| Vendor | What it adds | Pricing (2026) | Use case | Status |
|---|---|---|---|---|
| **Tiingo** "Business / Organization" | EOD price, fundamentals, 50+ year history; **explicit commercial redistribution license** | **$50/mo or $499/yr flat** | Daily price/OHLCV, 52w range, market cap, dividends, splits, sector ETF EOD | 🔌 stub — wire in when key issued |
| **Intrinio** Enterprise | US fundamentals, prices, options, mutual funds, ETF holdings | From $200/mo (US Equities bundle); Enterprise custom | Alternative to Tiingo; deeper US fundamentals | 🔌 stub |

### Tier 4 — Optional (only if budget allows)

| Vendor | What it adds | Pricing | Decision criterion | Status |
|---|---|---|---|---|
| **S&P Dow Jones Indices** | Index levels + sector perf | Custom (typically $5-15k/yr for redistribution) | If `portfolio_meeting` needs a true benchmark | 🔌 stub |
| **ICE/NYSE** historical | Tick-level US equity history | Custom (expensive) | Only for backtesting | — |
| **MSCI ESG** direct | ESG ratings | $15-30k/yr | Only if ESG is a regulatory requirement | — |

---

## What got built in this round (Phases 1, 3, 5, 6)

### Files created

```
src/calorch/
├── fred.py              # FRED API wrapper (VIX, 10Y, oil, gold, BTC, S&P, CPI, etc.)
├── fed_h15.py           # FOMC H.15 selected interest rates (no key required)
├── sec_ixbrl.py         # iXBRL instance parser → segment & geographic revenue
├── sec_efts.py          # SEC full-text search → guidance/outlook excerpts
└── providers.py         # Protocol-based dispatcher + free-source bundle

tests/
├── test_fred.py         # 13 tests: FRED/H.15 + macro provider
├── test_sec_providers.py # 10 tests: iXBRL parser + EFTS
└── test_providers.py    # 9 tests: provider dispatcher + config gating
```

### Files updated

- `pyproject.toml` — added `fredapi>=0.5`, `lxml>=5.0`
- `src/calorch/config.py` — added `FRED_API_KEY`, `USE_FRED`, `USE_IXBRL_SEGMENTS`, `USE_SEC_EFTS`, `USE_FED_H15`, `TIINGO_API_KEY`
- `src/calorch/tools.py` — added `make_providers()` and `make_cik_lookup()` factories
- `src/calorch/nodes.py` — Context now carries `providers` and `cik_lookup`; passed to `build_analysis`
- `src/calorch/renderers.py` — every per-event-type builder takes `providers` and `cik_lookup`; adds macro box, segment table, geography table, and EFTS guidance excerpts
- `scripts/run_demo.py` and `src/calorch/serve.py` — wire providers and cik_lookup into Context

### Test count: 45 passing (was 15, +30 new)

### Provider dispatcher logic

```python
def build_providers(settings) -> ProviderBundle:
    # Macro: FRED (real or stub) + FOMC H.15 (real or stub)
    # Segments: SEC iXBRL (real, parser) → stub fallback
    # Narrative: SEC EFTS (real, search) → stub fallback
    # Price: stub only (no free source approved)
    # Consensus: stub only (no free source — requires terminal)
    return ProviderBundle(price, consensus, macro, segments, narrative,
                         sources_active, sources_stub)
```

The dispatcher reads `Settings` and returns a `ProviderBundle` whose
attributes conform to `PriceProvider`, `ConsensusProvider`,
`MacroProvider`, `SegmentProvider`, `NarrativeProvider` `Protocol`s.

To swap a provider, **only the env vars change**:

```bash
# Enable real price data
export TIINGO_API_KEY=...           # adds TiingoPriceProvider
# Enable real consensus
export REFINITIV_CLIENT_ID=...      # adds RefinitivConsensusProvider
# or
export FACTSET_API_KEY=...          # adds FactSetConsensusProvider
# or
export BLOOMBERG_BLPAPI_HOST=...    # adds BloombergConsensusProvider
```

The renderer never knows which one is wired. The Protocol contract is
the seam.

---

## What is still NOT built (Phases 2 and 4)

| Phase | Work | Cost | Trigger |
|---|---|---|---|
| **Phase 2** — Tiingo Business adapter | New `src/calorch/tiingo.py` wrapping `tiingo.com/iex` + `tiingo.com/fundamentals` | $50/mo | Approve spend |
| **Phase 4** — Terminal vendor adapter (Refinitiv / FactSet / Bloomberg / S&P CI) | New `src/calorch/refinitiv.py` (or whichever) | already licensed | Pick vendor, issue key/host |

Both phases are stubbed today. The stub returns placeholders like
`{"price": None, "source": "stub", "note": "no price provider configured"}`
so the brief renders cleanly without a paid key.

---

## What is still not in scope (and how to handle it)

| Data | Why not | Workaround |
|---|---|---|
| **Insider transaction analytics** (Form 4 cluster buy/sell scoring) | Requires the firm's analytics layer (Bloomberg, Form4Oracle) | calorch ships raw Form 4 filings list; analyst pulls signals from existing tool |
| **Earnings call transcripts** | Bloomberg/Refinitiv have these but they're paywalled per-call | EFTS full-text search returns relevant 8-K/10-K/10-Q snippets; analyst clicks through for full transcript |
| **Management commentary / guidance** | Same as transcripts | EFTS search for "outlook", "guidance", "expect" returns curated excerpts |
| **Peer comparison** | Requires per-ticker loop | calorch does this for SEC (free); for the consensus side it depends on the provider |

These are **out of MVP scope** and do not block the build. The reference
prep_scanner output is the north star but not a strict requirement.

---

## Decision points still open

1. **Refinitiv vs FactSet vs Bloomberg** — which terminal does the firm actually have? (Pick one; cost is sunk.) → enables consensus, segments, transcripts
2. **Tiingo Business $50/mo** — approve? (Recommended yes; cleanest dedicated price source for the quote box.) → enables price, market cap, sector ETF perf
3. **FRED API key** — approve? (Free, 30-second signup, required for full macro box; H.15 already covers treasury rates without a key.) → enables VIX, S&P, oil, gold, BTC, CPI, unemployment

Recommend approving #2 + #3 in the next sprint, deferring #1 until a vendor pick.
