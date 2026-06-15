# Analyst-Grade Prep Packs — Design Brief

> Companion to the detailed phase plan in
> [`analyst-grade-report-templates.md`](./analyst-grade-report-templates.md).
> This brief is the stakeholder-facing summary: problem, requirements,
> proposed architecture, and delivery shape.
>
> **Status (2026-06-15):** plan approved, not yet implemented. The two
> latent live-mode bugs called out below have already been fixed and pushed
> on `claude/azure-durable-langraph-refactor-nndomb` (commit `a1eecf4`);
> the feature work remains parked.

## 1. Problem statement

Calorch generates equity-research "prep packs" (DOCX briefs + HTML email
digests) for eight calendar-driven event types. After a recent provider
cleanup that narrowed live data to **SEC EDGAR + AlphaSense only**, the
generated packs "feel superficial." Three root causes:

1. **Dead sections.** Consensus, valuation, price-performance,
   analyst-ratings and ESG sections survive in the templates but have no
   backing data source, so every row renders as `—`.
2. **Fabricated data.** Several templates carry invented content — fake
   analyst personas ("Dr. Sarah Chen", "Morgan Stanley"), mock portfolio
   holdings, hardcoded event times ("8:00 PM IST"), and a literal
   `"Q1 FY2026"` fiscal label.
3. **Shallow real data.** SEC extraction keeps only the *latest quarter* of
   15 metrics, even though the already-downloaded companyfacts JSON holds
   full multi-year history, cash-flow, liquidity, and capital-return data.

## 2. Requirements

- **Sources stay SEC + AlphaSense only.** Verified: the AlphaSense developer
  API exposes document search / sentiment / classification — no prices,
  estimates, or multiples. Valuation and price sections are therefore
  *dropped*, not re-sourced.
- **No fabricated content.** Every value is either real, derived from real
  data (and footnoted as such), or the row/section is omitted.
- **Go deep on the data we already have.** Multi-quarter trends, cash flow,
  liquidity, working-capital / channel-check metrics, capital returns.
- **The three "mock" templates** (portfolio_meeting, internal_review,
  kol_meeting) are **fixed with real data**, not removed.
- **Honest degradation preserved.** A missing source produces an omitted
  section, never an exception or a stub — the existing contract.
- **No regression to the Durable Functions orchestration** (still 13
  functions; data/template/renderer layers only).

## 3. Proposed architecture

The change is concentrated in the data-extraction, analytics, rendering, and
template layers. The provider Protocol boundary and the orchestrator are
untouched.

```
companyfacts JSON (cached)
        │
        ▼
sec_ixbrl.fundamentals_history()      ← NEW: multi-quarter/annual extraction
        │   {quarterly:[…], annual:[…]} with per-period provenance
        ▼
fin_metrics.py                        ← NEW: pure, None-safe derived analytics
        │   YoY/QoQ, FCF, margins, CCC/DSO/DIO/DPO, TTM, trend rows/strings
        ▼
agent builders (per event type)       ← produce data_tables + LLM context
        │
        ▼
templates.py engine + renderers.py    ← dash-row suppression, provenance
        │                                footnotes, full HTML digest
        ▼
DOCX brief  +  HTML email digest
```

**Key components**

- **`sec_ixbrl.fundamentals_history(cik, ticker, quarters=5, annual_years=3)`**
  — reuses the cached companyfacts fetch; dedupes by `(start, end)` keeping
  the latest-filed entry (restatements win); builds a quarter spine from
  revenue period-ends; derives a missing Q4 as `FY − (Q1+Q2+Q3)` (tagged
  `derived_q4`); reads fiscal labels from companyfacts `fy`/`fp` (kills the
  hardcode); attaches per-period provenance (form, filed date, accession).
  `latest_fundamentals()` is re-expressed as `quarterly[0]` for back-compat.
- **`fin_metrics.py`** — a new pure module of None-safe analytics so the
  math is testable in isolation and reusable across builders.
- **Engine / renderer upgrades** — dash-row suppression at the *engine*
  level (so DOCX and HTML both benefit), optional `source_note` provenance
  footnotes per table, and a full HTML email digest that walks all sections
  (capped, with "full detail in attached DOCX").
- **New blob path** `inputs/fundamentals_history/{cik}/{ticker}/{date}.json`
  with a latest-date fallback, so reports don't require same-day ingestion.
  Old blobs stay valid.

**Per-template direction (summary)**

- *earnings_call* (reference implementation): real last-quarter performance,
  quarterly trend, cash-flow & capital returns, balance-sheet liquidity,
  guidance from EFTS filings, AlphaSense documents + sentiment. Dead
  consensus/valuation/ESG/price rows removed.
- *management_meeting / conference*: drop price/CEO/consensus metadata; wire
  the trend + filings + sentiment tables the builders already produce.
- *channel_check*: real inventory days, DSO/DIO/DPO/CCC, FCF/CapEx/R&D from
  fin_metrics; persona rows dropped.
- *analyst_meeting / kol_meeting / portfolio_meeting / internal_review*:
  fabricated personas/holdings/stats deleted; counterpart and expert details
  derived from the calendar event (organizer/attendees/subject); portfolio
  and internal-review stats sourced from the watchlist and the delivery
  repository respectively, with honest omission when empty.

## 4. Delivery plan

Seven phases, implemented in order (full detail in the companion plan):

1. **SEC data depth** — `fundamentals_history`, new concepts, `fin_metrics`,
   plumbing + new blob path. *(Includes the two bug fixes — see below.)*
2. **Engine + renderer upgrades** — dash-row suppression, provenance
   footnotes, full HTML digest, optional delta coloring.
3. **+4. Template redesigns** — new `rows_from` data contracts; per-template
   redesign with earnings_call as the reference.
5. **Builder/context fixes** — real event date/time, ticker trend context,
   `ticker_context()` rewrite, LLM trend-string enrichment.
6. **Tests** — updated builder snapshots, new `test_fin_metrics`, history
   extraction tests, dash-suppression/footnote/digest renderer tests.
7. **Verification** — pytest + ruff green, 13 durable functions intact,
   wheel ships the modified templates, mock E2E DOCX eyeball.

## 5. Bug fixes already landed (Phase 1 advance)

Two latent live-mode defects found during research were safe to fix
independently and have been merged ahead of the feature work:

- **`search_for_guidance` name mismatch** — `providers.py` and
  `data_ingestion.py` called `search_guidance`, but `SecEftsClient` only
  defines `search_for_guidance`, raising `AttributeError` on any live
  filings fetch. Both call sites corrected.
- **`latest_fundamentals` ranking bug** — `reverse=(True, False, False)` is
  a truthy tuple that Python collapses to `reverse=True`, sorting every key
  descending and inverting the 10-Q form preference. Keys recoded so
  higher-is-better, with a real boolean `reverse`.

Full suite green after the fix.

## 6. Risks & mitigations

- **52/53-week fiscal calendars** — quarter/FY day-count windows
  (80–100d / 350–380d) validated against AAPL.
- **Banks lack GrossProfit/CostOfRevenue** — those rows omit cleanly.
- **Derived Q4 EPS is approximate** under buybacks — footnoted.
- **AlphaSense usually unconfigured** — sections visibly omit; the Data
  Sources table already discloses provider status.
- **Snapshot churn** — template edits and snapshot updates land in the same
  commit.
- **Outlook HTML size** — the email digest is capped.
