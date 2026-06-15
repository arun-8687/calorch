# Analyst-grade report templates (SEC + AlphaSense only)

> **Status:** approved plan, not yet implemented (parked 2026-06-12).
> Revisit by implementing the phases in order; Phase 1 also fixes two
> latent live-mode bugs and is safe to land on its own.

## Context

The prep packs "feel superficial" because: (1) **dead sections** — after the
provider cleanup, consensus/valuation/price-performance/analyst-ratings/ESG
sections render all `—`; (2) **fabricated data** — fake personas
("Dr. Sarah Chen", "Morgan Stanley"), mock portfolio holdings, invented
coverage stats, hardcoded event times and `"Q1 FY2026"` labels; (3)
**shallow real data** — SEC extraction keeps only the latest quarter of 15
metrics even though the already-downloaded companyfacts JSON contains full
multi-year history, cash-flow, liquidity, and capital-return concepts.

User decisions: sources stay **SEC + AlphaSense only** (verified: the
AlphaSense developer API exposes document search/sentiment/classification
only — no prices/estimates/multiples, so valuation/price sections are
dropped, not re-fed); the three mock templates (portfolio_meeting,
internal_review, kol_meeting) are **fixed with real data**, not removed.

Two latent bugs found during research, fixed in Phase 1:
- `providers.py:132` and `data_ingestion.py:164` call
  `self._efts.search_guidance(...)` but the method is
  `SecEftsClient.search_for_guidance` → live-mode `AttributeError`.
- `sec_ixbrl.py:497` `reverse=(True, False, False)` — a truthy tuple, so all
  sort keys reverse; tie-breakers prefer the wrong entries, and flow metrics
  can pick FY values as "the quarter".

## Phase 1 — SEC data depth

**`src/calorch/sec_ixbrl.py`**
- Replace `_MAP` with `_CONCEPTS: {key: ((synonyms…), unit, "duration"|"instant")}`.
  Add synonyms (`RevenueFromContractWithCustomerExcludingAssessedTax` first —
  AAPL/MSFT report this, plain `Revenues` is None for them today) and new
  concepts: `operating_cash_flow`, `cost_of_revenue`, `sga_expense`,
  `depreciation_amortization`, `interest_expense`, `income_tax`,
  `current_assets`, `current_liabilities`, `accounts_payable`,
  `buybacks` (PaymentsForRepurchaseOfCommonStock), `dividends_paid`.
- New `fundamentals_history(cik, ticker, *, quarters=5, annual_years=3)`
  reusing the cached `_fetch_companyfacts`:
  - dedupe entries by `(start, end)` keeping max `filed` (restatements win);
  - quarterly flows = durations 80–100 days; build the quarter spine from
    revenue period-ends; align other concepts by exact `end`;
  - **Q4 derivation**: missing Q4 = FY(350–380d) − ΣQ1..Q3 when all three
    present; tag `derived_q4: true` (EPS too, tagged `eps_derived` —
    approximate under buybacks, footnoted);
  - instants matched to quarter `end` (±10 days for 52/53-week issuers);
  - fiscal labels from companyfacts `fy`/`fp` → `"Q1 FY2026"` (kills the
    hardcode); per-period provenance: `form`, `filed`, `accession`,
    `period_start/end`.
  - Return `{quarterly: [newest-first period dicts], annual: [...]}` with
    per-period margins.
- Reimplement `latest_fundamentals()` as `quarterly[0]` + existing derived
  keys (fixes the ranking bug; same output keys for back-compat).

**New `src/calorch/fin_metrics.py`** — pure, None-safe derived analytics:
`yoy/qoq`, `margin_deltas`, `fcf` (= OCF − capex) / `fcf_margin`,
`current_ratio`, `working_capital`, `rd_pct_revenue`, `effective_tax_rate`,
`dso/dio/dpo/ccc` (channel checks), `ttm(history, key)` (needs 4 quarters),
`capital_returns`, `trend_rows(history)` (5-quarter table),
`trend_summary_strings(history)` (compact LLM context).

**Plumbing**
- `providers.py`: add `fundamentals_history` to the `FundamentalsProvider`
  Protocol + `IxbrlFundamentalsProvider`; **fix the `search_for_guidance`
  rename** here and in `data_ingestion.py`.
- New blob `inputs/fundamentals_history/{cik}/{ticker}/{date}.json` (old
  blobs stay valid); `blob_reader.py` provider reads it with a
  latest-date `list_blobs` fallback so reports don't require same-day
  ingestion; `data_ingestion.ingest_fundamentals` writes it.

## Phase 2 — Engine + renderer upgrades

- **Dash-row suppression (engine level)** — `templates.py`
  `_build_data_section` rows path + `_build_meta_table`: drop a row when the
  formatted value is empty after stripping `—`/punctuation (catches
  `"— (—/—/—)"`) or still contains `{unresolved}` placeholders; if all rows
  drop, omit the section. Engine level so DOCX + HTML both benefit and
  `blank_rows` note-grids stay untouched.
- **Provenance footnotes** — optional `source_note` on every table dict;
  `render_docx` emits italic 8pt gray under the table, `render_html_email`
  a small gray div. Builders fill e.g.
  `"Source: SEC 10-Q filed 2026-01-28, period ended 2025-12-27"`,
  `"Q4 derived as FY minus Q1–Q3"`, `"Source: AlphaSense, n documents"`.
- **Full HTML email digest** — rewrite `_render_html_email_inner` to walk all
  sections with tables interleaved (same `__TABLE__` sentinel logic as
  DOCX), capped (~8 bullets/section, ~10 tables) with "Full detail in
  attached DOCX" past the cap.
- **Delta coloring (DOCX)** — opt-in `colorize` table flag on trend/metric
  tables: `±x.x%`/`pts` cells get green/red run color.

## Phase 3+4 — Template redesigns + data_tables contracts

Templates in `src/calorch/data/templates/`; new `rows_from` keys produced by
builders (missing key ⇒ section omitted — existing degradation preserved):

| key | content |
|---|---|
| `quarterly_trend` | Metric × 5 fiscal labels: Revenue, YoY%, margins, EPS, FCF |
| `cash_flow` | OCF, CapEx, FCF, FCF margin, Buybacks, Dividends — latest Q + TTM |
| `guidance_filings` | EFTS hits: Date, Form, Excerpt (≤200 chars) |
| `narrative_docs` / `transcript_docs` | AlphaSense docs: Date, Type, Title |
| `sentiment_docs` | per-doc Date, Title, Type, Score + aggregate row |
| `watchlist` / `sentiment_overview` / `catalysts` | portfolio (below) |
| `ops_activity` / `watchlist_coverage` | internal review (below) |

- **earnings_call**: keep executive_snapshot (llm), last_quarter (now real:
  Revenue+YoY, EPS+YoY, margins with Δ vs PY), segments, geo, key_themes
  (llm). Add quarterly_trend, cash_flow_capital, balance_sheet+liquidity
  (current ratio/working capital now real), guidance_commentary
  (`guidance_filings`), recent_documents (`narrative_docs`),
  sentiment_detail (`sentiment_docs`). **Remove** consensus, valuation,
  analyst_sentiment, esg, price_performance, estimate/surprise rows.
- **management_meeting / conference**: metadata loses price/CEO/consensus
  rows (gains "Latest filing"); add quarterly_trend + filings evidence +
  sentiment sections (the builder already makes tables the template never
  referenced — wire them); financial_summary rows become Rev YoY, Op margin
  Δ, FCF margin, R&D %. Remove conference `esg_governance`.
- **channel_check**: key_metrics become REAL — inventory days (vs PY), DSO/
  DIO/DPO/CCC, FCF/CapEx/R&D/buybacks Q+TTM from fin_metrics; margin_profile
  columns = last/prev/prior-year quarter + TTM from history;
  metrics-to-validate grid seeded from trend data; contact/location from
  `ev.organizer`/`ev.location`/`is_online`; drop hardcoded persona rows.
- **analyst_meeting**: delete fabricated analyst_profile + quoted_view +
  static debate_points (→ llm "Debate Points" fed trend data); counterpart
  name/firm derived from event organizer/attendee (email domain) or `—`;
  add fundamentals_snapshot + quarterly_trend + sentiment_docs.
- **kol_meeting**: expert/affiliation/topic from event attendees/organizer/
  subject/body_preview ("Dr. Sarah Chen" deleted); add company_context
  (only when a ticker resolves) and expert_transcripts
  (`transcript_docs` — real `transcript_hits`, currently unused);
  discussion guide generalized (not healthcare-specific).
- **portfolio_meeting** (builder rewrite): drop market_context/
  sector_performance/holdings (no source). watchlist_snapshot +
  sentiment_overview over `settings.sec_watchlist` (cap ~8, per-ticker
  try/except), catalysts from EFTS filings across the watchlist,
  key_movers → "Fundamental Highlights" (llm, fed per-ticker trend strings).
- **internal_review** (builder rewrite): real ops stats from the delivery
  repository — add optional `ops` slot to `ProviderBundle` (default None,
  non-breaking) wrapping `make_repository(...)` with
  `activity_stats(days=90)`; pipeline_activity table (per event type:
  count/drafted/sent/avg confidence) + watchlist_coverage (latest period/
  form/filed per ticker). Fabricated "47 names / 12 initiations" deleted;
  empty repository ⇒ honest omission.

## Phase 5 — Builder/context fixes

- `analysis.py`: new `event_datetime_ctx(ev)` (real `event_date`/`event_time`
  from `ev.start`, replacing every hardcoded "8:00 PM IST") and
  `ticker_trends(ticker, providers, cik)` (history → fin_metrics → tables +
  ctx fields + labels + trend strings). **Rewrite `ticker_context()`**:
  delete the ~25 literal `—` market keys and hardcoded labels.
- LLM enrichment: builders add `revenue_trend`/`margin_trend`/`fcf_trend`
  strings, top-3 EFTS `guidance_excerpts` (inside `<<<DATA … DATA>>>`),
  AlphaSense `recent_doc_titles` to ctx; `llm_enrich._ctx_prompt` raises
  truncation to ~240 chars for `_trend`/`_excerpts` keys. Verify the
  `_THINKING_PHRASES` filter doesn't eat snippet-quoting output (test with
  MockChatModel). Grounding rules unchanged.

## Phase 6 — Tests (update, never delete)

- `test_agent_builders.py`: update snapshots (no-provider degraded shape) +
  add a stub-`ProviderBundle` case with a shared canned 5-quarter fixture
  pinning the fully-populated shape — the new regression net.
- `test_renderers.py`: adjust table-count assertions; add dash-row
  suppression, `source_note` (DOCX + HTML), and full-HTML-digest tests.
- New `test_fin_metrics.py`: Q4 derivation, EPS-derived flag, CCC/DSO/DPO,
  TTM needs 4 quarters, restatement dedupe.
- `test_sec_providers.py`: `fundamentals_history` vs synthetic companyfacts,
  fiscal labels from fy/fp, revenue-synonym priority, ranking fix.
- `test_providers.py`/`test_blob_store.py`: Protocol member, history blob
  round-trip + latest-date fallback. Internal_review/portfolio via stub
  `providers.ops` / monkeypatched watchlist.

## Phase 7 — Verification

1. `python -m pytest tests/ -q` and `ruff check src tests` — green.
2. `python -c "from function_app import app; print(len(app.get_functions()))"` → 13 (no durable changes).
3. Wheel gate: rebuild, confirm the 8 modified template JSONs ship.
4. Mock E2E eyeball: `USE_MOCKS=true` CLI run → open generated DOCX per
   event type; check: no dash-only rows, footnotes present, no literal
   `{placeholder}`, real event times/fiscal labels.
5. Commit + push to `claude/azure-durable-langraph-refactor-nndomb`.

## Risks

52/53-week fiscal windows (validate 80–100d / 350–380d live for AAPL);
banks lack GrossProfit/CostOfRevenue (rows omit — fine); Q4 EPS subtraction
approximate (footnoted); snapshot churn (template edit + snapshot update in
the same commit); AlphaSense usually unconfigured → sections visibly omit
(Data Sources table already discloses status); Outlook HTML size (capped).

Implementation order: Phase 1 (incl. both bug fixes) → 2 → earnings_call as
reference → management/conference/channel_check/analyst → 3 mock rewrites →
tests alongside → verification.
