# Azure Durable Functions — Applicability for calorch

> **⚠️ Superseded — historical evaluation.** calorch has since **adopted**
> the hybrid this document recommends: Azure Durable Functions as the
> orchestrator (flow control, fan-out/fan-in, approval gate, durable timers)
> with LangGraph multi-agent subgraphs running inside activities. The
> "should we switch?" question below is kept for the reasoning and trade-off
> analysis, but the answer in practice was **yes (hybrid)**. For the current
> design see [`docs/architecture.md`](../architecture.md) and the deployment
> guide [`deploy/azure-functions.md`](../../deploy/azure-functions.md). The
> "poor fit" caveats below applied to a *wholesale* replacement that kept no
> LangGraph; the shipped design keeps LangGraph exactly where determinism
> isn't required (inside activities), which resolves them.
>
> **2026-06-02 update:** the implementation now splits preparation,
> `approval_gate`, and delivery into separate LangGraph nodes. The gate uses
> `interrupt()` and can persist through `PostgresSaver` when
> `CHECKPOINT_POSTGRES_URI` is configured. Older references below to a single
> `per_event_pipeline` and `MemorySaver` describe the pre-hardening baseline.

> Evaluation based on a 2026 web search of Microsoft docs, cgillum.tech
> (Azure Functions team), build5nines, Diagrid, and the LangChain /
> Microsoft hybrid-pattern guidance. The question: should calorch use
> Durable Functions instead of LangGraph on Container Apps?

**Short answer:** No, not as a wholesale replacement. But a **hybrid**
deployment is viable if a team hits a real pain point with the current
container-based runtime (most likely the **multi-day human-approval
gate**). Details below.

---

## What calorch actually does

A weekly run ingests ~1,000 SEC filings + ~200 Outlook events, runs
the same 7-step pipeline on each in parallel, and persists to Cosmos.

| Per-event step | What it does | Wall-clock |
|---|---|---|
| 1. Enrich | `enterprise.fetch` (XBRL + mock) | 1–3s |
| 2. Analyze | `llm.with_structured_output` (Pass 2) | 1–5s |
| 3. Render DOCX | `python-docx` | <1s |
| 4. Upload OneDrive | HTTP PUT (2–3 MB) | 2–10s |
| 5. Build HTML email | jinja2 | <0.1s |
| 6. Send/draft | Graph `sendMail` or `createDraft` | 1–3s |
| 7. Patch calendar | Graph `patchEvent` | 0.5–2s |
| 8. Persist | Cosmos `upsert` | 0.1–0.5s |
| **Total per event** | | **~10–30s typical, 60s+ worst case** |

For a 1,000-event SEC run the fan-out completes in **~5 minutes** because
LangGraph's `Send` API runs N pipeline invocations concurrently in one
process.

---

## Why Durable Functions is a poor *fit* for the current design

### 1. Determinism requirement conflicts with the current code

Durable Functions orchestrators must be **deterministic** (Microsoft
docs, cgillum.tech). The current `per_event_pipeline` violates this in
several places:

```python
# ALL of these would need to move into separate activity functions:
c.enterprise.fetch(ev.subject, tickers=payload_tickers)  # HTTP I/O
c.onedrive.upload(...)                                    # HTTP I/O
c.graph.send_mail(...)                                    # HTTP I/O
c.repo.upsert(...)                                        # HTTP I/O
_now().isoformat()                                         # time
c.to_addresses or [a for a in ev.attendees if a] or ["..."]  # branches
```

The orchestrator would become a thin async-await wrapper, with every
`c.<service>.<call>` replaced by `yield context.call_activity(...)`.
**Net effect: ~250 lines of orchestrator code, replacing ~150 lines of
direct function calls.** The "control flow" abstraction gives you
nothing calorch doesn't already have.

### 2. The 5-min default timeout is uncomfortably tight

Per Microsoft docs (cited in build5nines, scaler.com):

| Plan | Default | Max |
|---|---|---|
| Consumption | **5 min** | 10 min (`host.json` `functionTimeout`) |
| Premium EP1 | 30 min | 60 min |
| Dedicated | 30 min | unlimited |

A typical event finishes in 10–30s, but a slow OneDrive upload during
peak hours or a 10-MB DOCX can push a single event to 60–90s. With 7
sequential `call_activity` calls per event, a hung OneDrive upload
during the 10-min cap means the orchestrator fails *mid-pipeline*, and
the **replay must redo everything that already succeeded**, which
requires idempotency keys on the repo upsert, the OneDrive upload
(if any), and the email send. None of that exists today.

### 3. Parallel fan-out is verbose in DF

LangGraph's fan-out is one line:

```python
sends.append(Send("per_event_pipeline", {"event": ev, "classification": cls}))
```

In Durable Functions, the equivalent requires:

```python
# Per-event sub-orchestrator
@app.orchestration_trigger(context_name="context")
def per_event_orchestrator(context):
    ev = context.get_input()
    cls = context.get_input()["classification"]
    enriched = yield context.call_activity("enrich", ev)
    analyzed = yield context.call_activity("analyze", enriched)
    docx_url  = yield context.call_activity("render_and_upload_docx", analyzed)
    email_id  = yield context.call_activity("draft_email", analyzed)
    yield context.call_activity("patch_calendar", email_id)
    yield context.call_activity("persist", email_id)
    return ...

# Fan-out from the top-level orchestrator
tasks = [context.call_sub_orchestrator("per_event_orchestrator", e) for e in events]
results = yield context.task_all(tasks)
```

That's **~25 lines of boilerplate per workflow step**, plus a separate
`function.json` (or Python v2 decorator) per activity, plus a binding
config per service (Cosmos, Graph, OneDrive). For 8 workflow types
this scales to **8 sub-orchestrators × 7 activity functions = 56
function declarations** to manage.

### 4. Replay model adds hidden complexity

Durable Functions re-runs the orchestrator from the beginning on every
resume, replaying all `call_activity` calls until it reaches the new
state. Each replay re-executes the orchestrator code, re-evaluates
`if/else` branches, and re-iterates over any Python data structures in
the orchestrator's local variables. The longer the orchestration, the
more memory + CPU per replay. For calorch's 1,000-event run, that's
**~7,000 replay cycles on the parent orchestrator per resume**, even
though we never resume it (the parent is fire-and-forget).

To bound this, Microsoft recommends `ContinueAsNew` to reset the
history every N events. calorch would need that plus event-count-aware
resumption logic — extra moving parts that LangGraph handles natively
via its `MemorySaver` checkpointer.

### 5. Cost is similar but less predictable

Both run in the $5–10/mo range for a weekly job. The difference:

| Cost driver | LangGraph on ACA | Durable Functions |
|---|---|---|
| Idle | $0 | $0 (Consumption) |
| Active compute | per-second billing, ~$0.30/wk | per-action billing, $0.20/M actions |
| Storage | Cosmos (~$0.25/GB) | Cosmos + Azure Storage task hub (~$0.10/GB) |
| Replay cost | $0 (no replays) | bounded by history size; can spike on long orchestrations |
| Predictability | linear in wall-clock | linear in *steps*, not wall-clock |

The DF cost is harder to predict because **replays on every activity
completion** mean a 1,000-event run creates 7,000+ orchestrator
invocation-seconds of replay history, even though only 1,000 events
actually happened.

---

## Where Durable Functions *does* win for calorch

The one scenario where Durable Functions is clearly better is the
**long-duration human-approval gate**.

Today calorch handles this two ways (D2 in IMPLEMENTATION_REVIEW.md):

1. `Context.send_emails = False` (default) — creates drafts, not sends.
2. `interrupt_before=["per_event_pipeline"]` — pauses the entire graph.

If a workflow needs to wait **days** for a senior analyst to approve
a complex channel-check survey (the original ADR mentioned 48h waits),
then:

- **LangGraph checkpointer** lives in process memory (`MemorySaver`).
  On container restart, all in-flight reviews are lost. To persist
  them you'd need a `PostgresSaver` or `CosmosDBSaver` — the latter is
  now officially supported by LangGraph (as of late 2025), but it's
  less battle-tested than Durable Functions' built-in task hub.

- **Durable Functions** has first-class support for `WaitForExternalEvent`
  with arbitrary timeouts (days/weeks), durable timers, and automatic
  cleanup. If the orchestration never resumes, the task hub cleans up
  the state after the configurable "fire-and-forget" timeout.

So the recommendation splits cleanly:

### Current scope (weekly 1,000-event run, <1h total)
**Stay on LangGraph + ACA.** The current design is correct and
cost-effective. The 5-min DF timeout isn't an issue because we never
hold a single event that long, and the 1,000-event run completes in
5 min — well within ACA's per-revision lifetime.

### If/when calorch grows multi-day human-in-the-loop workflows
**Migrate to a hybrid:** keep the LangGraph StateGraph inside the
activity, but wrap the entire run in a Durable Functions orchestrator
that handles the long-duration resume + durable timer + cross-run
state.

This is the pattern Microsoft and LangChain are converging on in 2026
(per the searches): **Durable Functions as the outer reliability
shell, LangGraph as the inner agentic brain.** For calorch this would
look like:

```
Durable Function (orchestrator)
  ├─ Wait for cron / HTTP trigger
  ├─ call_activity: scan_calendar (LangGraph inside this activity)
  ├─ call_activity: classify_all (LangGraph inside this activity)
  ├─ for each event in parallel:
  │    call_sub_orchestrator: per_event_pipeline_df
  │      ├─ call_activity: enrich (LangGraph node)
  │      ├─ call_activity: analyze (LangGraph node)
  │      ├─ call_activity: render_docx
  │      ├─ call_activity: upload_onedrive
  │      ├─ call_activity: draft_email
  │      ├─ call_activity: patch_calendar
  │      ├─ call_activity: persist_cosmos
  │      └─ WaitForExternalEvent: human_approval  ← days-long pause
  └─ call_activity: aggregate_briefing (LangGraph node)
```

**The cost of doing this hybrid: ~6 weeks of dev work** to wrap each
node in an activity function, add idempotency keys everywhere, build
a DF starter workflow, and add a `WaitForExternalEvent` approval API.
For a system that runs once a week and completes in 5 min, that's a
negative ROI.

---

## Verdict

| Scenario | Recommendation |
|---|---|
| Current calorch (weekly 1,000-event run) | **Stay on LangGraph + ACA** ✓ |
| Add multi-day human-approval to one workflow | **Add `CosmosDBSaver` to LangGraph** (1 day of work) — keeps it in-process but persists state |
| Need cross-team scheduling + cross-org SLA tracking | **Hybrid: DF outer + LangGraph inner** (6 weeks) |
| 100% rewrite of calorch from scratch | **DF on Premium plan** is fine if no LLM in the orchestrator |
| Need 50,000+ events/day at production scale | **Either** — both scale linearly; DF replay cost becomes significant |

For calorch's actual use case (8 workflows, 1,000 events/week, <1h
duration, no multi-day pauses), the LangGraph-on-ACA design is
**objectively a better fit** than Durable Functions. The web-search
consensus in 2026 confirms: choose the framework that matches the
*shape* of the workflow, not the framework that has the most features.

---

## Sources

- Microsoft Learn: Durable Functions overview, orchestrator code constraints
  https://learn.microsoft.com/azure/azure-functions/durable/
- Chris Gillum (Azure Functions team): orchestrator replay model
  https://cgillum.tech/
- build5nines: timeout limits by hosting plan
- Diagrid: durable execution patterns for AI
- Microsoft / LangChain hybrid architecture guidance (2025–2026)
- LangGraph 0.2+ checkpointers: MemorySaver, PostgresSaver, CosmosDBSaver
