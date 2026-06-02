# SEC EDGAR coverage & gaps — what needs supplementing

> **Question:** What does SEC EDGAR provide for free, and what gaps must be
> filled by enterprise data providers (Refinitiv RDP / FactSet / Bloomberg
> BQL / S&P Capital IQ / Tiingo / FRED)?
>
> **Method:** I read every calorch renderer section (in
> `src/calorch/renderers.py:130-499`) to identify the exact data fields
> the prep-packs require, then checked each field against SEC EDGAR's
> official API surface.

---

## What SEC EDGAR has (free, ToS-compliant, no auth)

### 1. Filings metadata — `https://data.sec.gov/submissions/CIK{cik}.json`

Already used in `src/calorch/sec.py:170` and `:336`.

| Field | Available? | Example |
|---|---|---|
| Form type | yes | `"8-K"`, `"10-K"`, `"10-Q"`, `"4"`, `"13F-HR"`, `"DEF 14A"` |
| Filing date / accession | yes | `"2026-04-30"` / `"0000320193-26-000123"` |
| Primary document URL | yes | `https://www.sec.gov/Archives/edgar/data/...` |
| Items string (8-K) | yes | `"2.02"`, `"5.07"`, `"9.01"` |
| Issuer name + CIK | yes | `"Apple Inc."` / `"0000320193"` |
| Form-specific items (10-K, 10-Q) | partial | not as structured; need full-text search |
| **Filing body text** | yes | `Archives/edgar/data/{cik}/{accn}/{filename}` (HTML) |

**Already used for:** `_form`, `_ticker`, `_accession`, `_filingDate`, `_items` in `sec.py:263-284`.

### 2. XBRL company facts (consolidated) — `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`

Already used in `src/calorch/sec.py:171` and `:287`.

| Field | Available? | Notes |
|---|---|---|
| **Total Revenue** | yes | tag `Revenues` or `RevenueFromContractWithCustomerExcludingAssessedTax` |
| **Net Income** | yes | tag `NetIncomeLoss` |
| **Diluted EPS** | yes | tag `EarningsPerShareDiluted`, unit `USD/shares` |
| **Total Assets / Liabilities / Equity** | yes | tags `Assets`, `Liabilities`, `StockholdersEquity` |
| **Operating Income / Cost of Revenue / R&D / SG&A** | yes | standard us-gaap tags |
| **Cash & equivalents** | yes | tag `CashAndCashEquivalentsAtCarryingValue` |
| **Total debt** | yes | tag `LongTermDebt` or `LongTermDebtNoncurrent` |
| **Shares outstanding (basic & diluted)** | yes | tags `CommonStockSharesOutstanding`, `WeightedAverageNumberOfDilutedSharesOutstanding` |
| **Quarterly / annual cadence** | yes | `fp` field: `FY`, `Q1`, `Q2`, `Q3`, `Q4` |
| **Point-in-time vs. duration** | yes | `start`/`end` date fields |
| **As-of filing date** | yes | `filed` field — important for backtesting |
| **Restatement markers** | partial | `frame` field links concept-period to filing |
| **Segment revenue** (iPhone/Mac/Services) | **partial — iXBRL only** | Not in `companyfacts.json`; only in raw iXBRL instance XML |
| **Geographic revenue** (Americas/EMEA/APAC) | **partial — iXBRL only** | Same as above |
| **Forward guidance** | no | Not structured; buried in 8-K narrative |
| **Per-customer / per-product breakdown** | no | Companies increasingly omit or aggregate |

**Already used for:** `revenue`, `net_income`, `eps_diluted`, `revenue_period`, `revenue_form` in `sec.py:322-333`.

### 3. Frames API (cross-company) — `data.sec.gov/api/xbrl/frames/`

| Field | Available? |
|---|---|
| All filers for one concept in one period | yes |
| Useful for "all S&P 500 RevenueThisPeriod" | yes |
| Already used? | **No** — not needed for current calorch scope |

### 4. Full-text search (EFTS) — `efts.sec.gov/LATEST/search-index?q=...`

| Field | Available? |
|---|---|
| Keyword search across all filings | yes |
| Returns accession #s, snippets, filing date | yes |
| Used for: "Apple guidance AI" → finds 10-K MD&A paragraph | yes |
| Already used? | **No** — not yet wired |

### 5. Form 4 insider transactions — `data.sec.gov/submissions/CIK{cik}.json` (`form: "4"`)

| Field | Available? |
|---|---|
| Insider name, role, transaction date | yes |
| Shares traded, price, code (P/S/A/M) | yes |
| Direct vs. indirect ownership | yes |
| Cluster analytics (3 buys in 2 weeks) | no — must compute |
| Already used? | Form 4 listed in `_FILING_TYPE_MAP` as `analyst_meeting` event |

### 6. 13F institutional holdings — `data.sec.gov/submissions/CIK{cik}.json` (institution CIKs)

| Field | Available? |
|---|---|
| Quarterly holdings (CUSIP, shares, value) | yes (in 13F-HR XML, not in submissions JSON) |
| Used for: "Did Berkshire add AAPL this quarter?" | yes, requires 13F-HR XML parse |
| Already used? | listed as `portfolio_meeting` event in `_FILING_TYPE_MAP` |

### 7. Ticker ↔ CIK map — `www.sec.gov/files/company_tickers.json`

Already used in `src/calorch/sec.py:99-139`.

### 8. iXBRL instance documents (raw) — `Archives/edgar/data/{cik}/{accn}/{filename}_htm.xml`

| Field | Available? |
|---|---|
| Segment revenue, segment income, segment assets | yes (in `<xbrli:segment>` blocks) |
| Geographic revenue | yes (in custom-typed dimensions) |
| Goodwill by reporting unit | yes |
| **Parse cost:** 2-3 MB XML, ~1s per filing with lxml | cost is real |
| **Coverage:** ~60% of large US filers publish well-formed iXBRL | partial |
| Already used? | **No** — `sec_segments.py` is on the roadmap (Phase 5) |

---

## What calorch renderers ask for (the demand side)

Reading `src/calorch/renderers.py:130-499` shows every field a prep-pack
needs. Mapping each to a source:

### Earnings Call prep pack (`_build_earnings_call`, line 334)

| Section | Field | SEC has it? | Supplement needed |
|---|---|---|---|
| Quote box | revenue, net_income, eps_diluted | yes (XBRL) | none |
| Quote box | **market cap** | no | **Tiingo** or RDP |
| Quote box | **52-week range** | no | **Tiingo** or RDP |
| Quote box | **1W/1M/YTD return** | no | **Tiingo** or RDP |
| Estimates table | **consensus EPS (current Q, next Q, FY)** | no | **Refinitiv I/B/E/S** or FactSet Estimates |
| Estimates table | **consensus revenue (current Q, next Q, FY)** | no | **Refinitiv I/B/E/S** or FactSet Estimates |
| Estimates table | **price target (mean / high / low)** | no | **Refinitiv I/B/E/S** or FactSet Estimates |
| Estimates table | **EPS surprise % (last 4Q)** | no | **Refinitiv I/B/E/S** or FactSet Estimates |
| Estimates table | **# of analysts (Buy/Hold/Sell)** | no | **Refinitiv I/B/E/S** or FactSet Estimates |
| Segment table | segment revenue (iPhone/Mac/Services etc.) | **iXBRL only** | **RDP / FactSet** (cleanest) or iXBRL parser |
| Filing summary | transcript excerpt | partial (8-K narrative) | **Bloomberg / RDP transcripts** (full) or SEC 8-K excerpt |
| Forward commentary | guidance | partial (8-K) | **RDP / transcripts** |
| Capital allocation | buyback, dividend | yes (XBRL tags) | none |
| Watch | peer comp | partial (must loop SEC for each peer) | **RDP / FactSet** (pre-computed) |
| Watch | **P/E (TTM, forward), EV/EBITDA** | no (can compute but no consensus) | **RDP / FactSet / Tiingo fundamentals** |
| Watch | **beta** | no | **Tiingo / RDP** |

### Management Meeting prep pack (`_build_management_meeting`, line 374)

| Section | Field | SEC has it? | Supplement needed |
|---|---|---|---|
| Company snapshot | revenue, NI, EPS, **market cap, beta** | partial | **RDP / Tiingo** |
| Topic talking points | segment context | iXBRL only | **RDP** |
| Knowledge gaps | guidance / management views | no (except in transcript) | **RDP transcripts** |
| Catalysts | upcoming earnings date, consensus | partial (date from EDGAR, consensus from RDP) | **RDP** |
| Catalysts | **insider transactions (90d net)** | yes (Form 4 raw) | compute cluster signal |
| Catalysts | **institutional ownership change (13F)** | yes (13F-HR XML) | compute deltas |
| Catalysts | **macro context** (rate environment, sector perf) | no | **FRED + Tiingo** (or RDP) |

### Conference prep pack (`_build_conference`, line 398)

| Section | Field | SEC has it? | Supplement needed |
|---|---|---|---|
| Subject profile | revenue/NI history | yes (XBRL) | none |
| Subject profile | segment mix | iXBRL only | **RDP** |
| Recent disclosures | 8-Ks in last 90d | yes (SEC list) | none |
| Q&A preparation | **expert background (network intel)** | no | **firm's own Rolodex / LinkedIn Sales Navigator** |
| Q&A preparation | **consensus vs. contrarian view** | no (no consensus) | **RDP / FactSet** |
| Sell-side coverage | analyst names + conviction | no | **RDP / FactSet Estimates** |
| Targets | investor targets, intros needed | no | firm's CRM |

### Channel Check (`_build_channel_check`, in renderers)

| Section | Field | SEC has it? | Supplement needed |
|---|---|---|---|
| Distributor signals | sell-in vs. sell-through | no | **firm primary research** |
| Distributor signals | inventory levels | no | **firm primary research** |
| Comparable | **peer revenue trend** | yes (loop SEC per peer) | RDP faster |
| Comparable | **peer consensus** | no | **RDP** |
| Macro | sector index, VIX, 10Y, oil | no | **FRED** |
| Sell-side view | **most recent rating change** | no | **RDP / FactSet** |

### Analyst Meeting (`_build_analyst_meeting`, line 494)

| Section | Field | SEC has it? | Supplement needed |
|---|---|---|---|
| Analyst profile | coverage, rank, call accuracy | no | **RDP / FactSet** (analyst-level) |
| Recent notes | **recent rating change, target change** | no | **RDP / FactSet** |
| Quoted view | current view | no | **RDP / FactSet** |
| Cross-check | **consensus vs. analyst's view** | no (consensus from RDP) | **RDP** |
| Talking points | **recent Form 4 / 13F flow** | yes (SEC raw) | compute signal |

### KOL Meeting (`_build_kol_meeting`)

| Section | Field | SEC has it? | Supplement needed |
|---|---|---|---|
| KOL bio | publications, prior roles | no | firm Rolodex / public bio |
| Position in market | institutional holder count | yes (Form SC 13G/D) | none |
| Recent activity | SC 13G/D filings | yes (SEC list) | none |
| Talking points | **peer consensus + our position** | no | **RDP** |

### Portfolio Meeting (`_build_portfolio_meeting`)

| Section | Field | SEC has it? | Supplement needed |
|---|---|---|---|
| Holdings summary | each holding's revenue/NI/EPS | yes (SEC) + needs price | **Tiingo / RDP** |
| Holdings summary | **each holding's market cap, return YTD** | no | **Tiingo / RDP** |
| Holdings summary | **each holding's consensus, target** | no | **RDP** |
| Top movers | price movers today | no | **Tiingo** (intraday) or RDP |
| 13F deltas | institutional flows (since last meeting) | yes (13F-HR XML) | compute |
| **Benchmark** | **S&P 500 level + 1M return** | no | **FRED** (`SP500`) or RDP |
| **Risk metrics** | **beta, vol, max DD per holding** | no | **Tiingo** historical |

### Internal Review (`_build_internal_review`)

| Section | Field | SEC has it? | Supplement needed |
|---|---|---|---|
| Filing analysis | 11-K, 10-K/A, 20-F, 40-F, 6-K | yes | none |
| Restatement | XBRL `frame` / `filed` deltas | partial | SEC supports this |
| Internal commentary | firm views | no | firm's notes |

### Weekly Briefing (`_build_weekly_briefing`)

| Section | Field | SEC has it? | Supplement needed |
|---|---|---|---|
| Week ahead | earnings calendar (date + ticker) | yes (filing dates) + macro events | add macro from **FRED** calendar |
| Week ahead | **expected EPS for upcoming reports** | no | **RDP** (pre-announced consensus) |
| Macro snapshot | **VIX, 10Y, oil, gold, BTC, S&P 1W** | no | **FRED** |
| Sector performance | **XLK, XLF, XLE, XLV, XLY, XLP, XLI, XLU, XLC, XLB 1W** | no | **Tiingo ETF EOD** or RDP |

---

## Gap summary — categorical

### Category A: SEC has it. Use it. Cost = $0.

| Field | API |
|---|---|
| Filings metadata | `submissions/CIK{cik}.json` |
| Total revenue, NI, EPS, balance sheet | `companyfacts/CIK{cik}.json` |
| Ticker → CIK | `files/company_tickers.json` |
| Form 4 raw | `submissions/CIK{cik}.json` |
| 13F-HR raw | `Archives/.../{13F-HR}.xml` |
| 8-K filing body (HTML) | `Archives/.../{filename}.htm` |
| Filing full-text search | `efts.sec.gov/LATEST/search-index` |
| Frame cross-company | `api/xbrl/frames/...` |

### Category B: SEC has it, but parser needed.

| Field | Source | Effort |
|---|---|---|
| Segment revenue (iPhone/Mac/Services) | iXBRL `ProductOrServiceAxis` | medium (lxml, ~1s/file) |
| Geographic revenue | iXBRL `StatementBusinessSegmentsAxis` | medium |
| Goodwill by reporting unit | iXBRL | medium |
| 13F holdings detail | 13F-HR XML parse | medium |
| Form 4 cluster analytics | Form 4 XML parse + window math | easy |

### Category C: SEC does NOT have it. Need paid provider.

| Field | Required by | Provider(s) |
|---|---|---|
| Real-time / delayed price | quote box, weekly briefing | **Tiingo IEX** (15-min delayed) or RDP `Pricing` |
| 52-week range | quote box | Tiingo / RDP |
| Market cap | quote box | Tiingo / RDP |
| 1W / 1M / YTD return | quote box, weekly briefing | Tiingo / RDP (compute) |
| Beta | watch, risk | Tiingo historical / RDP |
| Analyst consensus EPS / revenue | estimates table | **RDP I/B/E/S** or **FactSet Estimates** |
| Price targets (mean/high/low) | estimates table | RDP / FactSet |
| Buy / Hold / Sell counts | estimates table | RDP / FactSet |
| EPS surprise % | estimates table | RDP / FactSet |
| Pre-cleaned segment revenue (vs. iXBRL) | segment table | RDP / FactSet |
| Pre-cleaned geographic revenue | segment table | RDP / FactSet |
| Earnings call transcripts | filing summary | RDP / FactSet / Bloomberg |
| Recent rating changes | analyst prep | RDP / FactSet |
| Sell-side coverage list | analyst prep | RDP / FactSet |
| Analyst-level call accuracy | analyst prep | RDP / FactSet |
| Peer pre-built fundamentals | watch | RDP / FactSet |

### Category D: Government free APIs (FRED + Federal Reserve).

| Field | Source | Cost |
|---|---|---|
| VIX | FRED `VIXCLS` | free with key |
| 10Y Treasury yield | FRED `DGS10` | free with key |
| Oil (WTI) | FRED `DCOILWTICO` | free with key |
| Gold | FRED `GOLDAMGBD228NLBM` | free with key |
| BTC | FRED `CBBTCUSD` | free with key |
| S&P 500 | FRED `SP500` | free with key |
| Fed Funds rate | Federal Reserve H.15 | free, no key |
| CPI / unemployment / GDP | FRED | free with key |
| Sector ETF EOD | **Tiingo** (or FRED for some) | Tiingo $50/mo |

---

## What `calorch` should do at MVP

For the demo to match `prep_scanner` detail level:

1. **Use SEC for filings, fundamentals, Form 4, 13F** — already done.
2. **Add a thin iXBRL segment parser** for ~10 demo tickers (AAPL, MSFT, GOOGL, AMZN, NVDA, JPM, GS, BAC, XOM, CVX) — 3-4 hours of work.
3. **Add a `consensus/quote/segment/macro` stub** with three implementations:
   - **Real mode (refinitiv/factset/bloomberg):** real data, behind a vendor-pick config flag
   - **Tiingo mode:** delayed price + EOD fundamentals (best price-only fallback)
   - **SEC-only mode:** the current state, with explicit "no consensus / no price" placeholders
4. **Add FRED macro client** for the weekly briefing macro box (1 hour).
5. **Add Tiingo client** for the quote box (3 hours, $50/mo).

This gives calorch a clean 4-mode adapter with graceful degradation:

```
if config.REFINITIV_API_KEY:     # full enterprise
    use Refinitiv client
elif config.FACTSET_API_KEY:
    use FactSet client
elif config.BLOOMBERG_HOST:
    use BLPAPI client
elif config.TIINGO_API_KEY:      # $50/mo, EOD + delayed price
    use Tiingo client
elif config.FRED_API_KEY:        # free, macro only
    use FRED client + SEC fallback for everything else
else:                            # demo, no keys
    use SEC-only with explicit "no data" placeholders
```

Each stub is **per-field**, so we can mix providers: RDP for consensus, Tiingo
for price, FRED for macro, SEC for filings. This is the right pattern for
enterprise deployment.

---

## Implementation status (2026-06-02)

Two of the "Category B: needs parser" gaps are now **shipped** in calorch:

| Field | Status | File |
|---|---|---|
| **Segment revenue** (iPhone/Mac/Services) | ✅ Real iXBRL parser live | `src/calorch/sec_ixbrl.py` |
| **Geographic revenue** (Americas/EMEA/APAC) | ✅ Real iXBRL parser live | `src/calorch/sec_ixbrl.py` |
| **EFTS full-text guidance** (8-K/10-K narrative excerpts) | ✅ Real EFTS client live | `src/calorch/sec_efts.py` |
| **Macro context** (VIX, 10Y, oil, gold, BTC, S&P) | ✅ Real FRED + FOMC H.15 | `src/calorch/fred.py`, `src/calorch/fed_h15.py` |
| Form 4 cluster analytics | 🔌 Out of scope (firm's analytics layer) | — |
| 13F holdings parse | 🔌 Out of scope (use Bloomberg/RDP) | — |
| Real-time / delayed price | 🔌 Stub only (no free source approved) | `src/calorch/providers.py:StubPriceProvider` |
| Analyst consensus | 🔌 Stub only (no free source — requires terminal) | `src/calorch/providers.py:StubConsensusProvider` |
| Earnings call transcripts | 🔌 Stub only (paywalled) | — |
| Pre-cleaned segment revenue at scale | 🔌 Stub only (RDP/FactSet cover this) | — |

The provider dispatcher in `src/calorch/providers.py` selects the real or
stub implementation per `Settings`. To swap, only the env vars change.

## What's truly impossible without a paid terminal?

Three things only Refinitiv/FactSet/Bloomberg can provide, with no free
or low-cost alternative:

1. **Analyst consensus** (EPS / revenue estimates, Buy/Hold/Sell counts,
   price targets, surprise %).
2. **Clean segment & geographic revenue** at scale (RDP covers 60K
   companies, FactSet ~70K, all normalized; iXBRL is the only free source
   but requires per-company parsers).
3. **Earnings call transcripts** (the actual spoken text, not the 8-K
   narrative).

These three categories are the **only** things requiring the terminal.
Everything else has a free or cheap alternative.

---

## Decision points

1. **Approve Tiingo Business $50/mo** (recommended) → enables quote box, weekly briefing, sector ETF snapshot.
2. **Approve FRED API key** (free) → enables macro box in weekly briefing.
3. **Choose terminal vendor** (Refinitiv / FactSet / Bloomberg / S&P CI) → for consensus, segments, transcripts. Vendor already in your firm; pick whichever has Python SDK the team is most familiar with.
4. **Phase 5 (iXBRL segment parser) — skip or do?** → skip if terminal is available; do if not.

Tell me which to start on and I'll begin.
