# Parked: Bigdata.com as a market-context provider backend

> **Status: PARKED (2026-06-15)** by user request — captured for later,
> not scheduled. Revisit when the qualitative/quantitative-context gap
> becomes the priority.

## Why

The report templates deliberately dropped consensus, valuation, price,
analyst-sentiment, and macro sections because SEC + the free backends have
no source for them. **Bigdata.com** (already connected to the working
session as an MCP server) does: company tearsheets, document-level
sentiment, an events calendar, and natural-language document/news search.
It is the natural way to re-enable those deleted sections with a real,
licensed source.

## Shape (fits the existing pattern)

Same Protocol-backend approach used three times already (edgartools,
SEC-narrative, lexicon-sentiment):

- New `src/calorch/bigdata.py` client wrapping the Bigdata.com API
  (`find_securities` → `rp_entity_id`, then `company_tearsheet`,
  `sentiment_tearsheet`, `events_calendar`, `bigdata_search`).
- New provider slots / backends selectable via config
  (`SENTIMENT_BACKEND=bigdata`, a new `events`/`context` slot), degrading
  to omission when unconfigured — never a hard dependency.
- Ingestion writes Bigdata-derived context to blob under new `inputs/…`
  paths; blob reader serves them; templates gain (re-add) the
  market-context / events / analyst-sentiment sections fed from them.
- Branding requirement: any report surface citing this source must use the
  exact string "Bigdata.com" and link https://bigdata.com.

## Open questions to resolve before building

- Licensing/redistribution terms for embedding Bigdata.com content into
  generated prep packs delivered by email.
- Whether the MCP tools are reachable from the headless ingestion runtime
  (interactively-authenticated MCP servers may be absent in cron/headless
  Azure runs) — may need a server-to-server API path instead of MCP.
- Which dropped sections to restore first (analyst sentiment + events
  calendar are the highest-value, lowest-ambiguity).
