# Phase 10 — Flagship Demo

**Status:** complete and validated live against the running stack.
**Scope:** one flagship enterprise scenario, a synthetic knowledge base, a
deterministic seed, real end-to-end execution, tests, and documentation.

---

## 1. What problem the demo solves

AegisForge is an execution platform, not a chatbot. Phase 10 proves that claim
on one realistic decision rather than on a toy prompt.

> **Acme Systems** (fictional) must decide whether to replace its self-managed
> data processing platform with a commercial streaming platform. Two candidates
> have been briefed: *Northwind Streamline* and *Helios Fabric*. The decision
> has to be made against internal architecture, security, data-governance,
> operational and cost requirements — and the two vendors' published claims
> conflict with those requirements in ways that matter.

Answering that question requires all of: decomposition, private retrieval,
tool-mediated research, structured comparison, explicit conflict surfacing, and
a defensible recommendation. No single LLM call produces it, and no answer
without provenance is worth acting on.

## 2. Architecture as demonstrated

```
USER REQUEST  (Execute composer, real form)
      ↓
PLANNER        deterministic rule planner — no special case for the demo
      ↓
TASK GRAPH     4 tasks / 3 waves
      ↓
┌───────────────────────┬───────────────────────────┐
│ RESEARCH agent        │ RAG agent                 │   wave 1 (parallel)
│ knowledge.search tool │ tenant-scoped retrieval   │
└──────────┬────────────┴─────────────┬─────────────┘
           ↓                          ↓
        ANALYSIS agent  (per-source evidence, conflict + gap detection)
           ↓
        SYNTHESIS agent  (grounded report, ranked citations, evidence quality)
           ↓
EVALUATION     ResultEvaluator + evidence assessment + 3-domain workflow
               evaluation (planning / collaboration / final response)
           ↓
        FINAL REPORT  →  citations, conflicts, gaps, measured confidence
```

Evaluation is a real workflow stage, not a decoration. There are two separate,
measured concepts, and the workspace makes that explicit:

- **Node-level evaluation** — `evaluate_node` scores the final agent result with the
  deterministic `ResultEvaluator` (verdict/score/reasons), plus optional LLM critic and
  evidence assessment. This drives retry/approval routing.
- **Workflow-level evaluation** — `evaluate_workflow_run` scores planning, collaboration,
  and final-response quality from the run records. It is exposed at
  `GET /api/v1/workflows/{id}/evaluations` and rendered in the workspace.

The Execution Workspace renders the **workflow-level** evaluation grid, not the
node-level verdict.

### Why the request is worded the way it is

The deterministic planner recognises compound intent *shapes*. The canonical
request is phrased so it hits the existing `enterprise knowledge research`
shape, which yields exactly the graph above. No planner rule was added for the
demo. The one planner value that did change (`top_k` for the RAG leg, 4 → 8) is
a general retrieval-coverage improvement, covered by a regression test.

## 3. Demo dataset

Nine synthetic markdown documents under [`demo/data`](../demo/data) — see
[`demo/README.md`](../demo/README.md) for the full list and the conflict table.
Requirements are numbered (`AR-1`, `SB-4`, `DG-3`, `IC-4`, `TP-5`) so findings
can cite them; vendor briefs deliberately contradict several of them.

Nothing is copied from a real organisation or vendor, and no external website is
contacted.

## 4. Seeding

```bash
python scripts/seed_demo.py --email demo@example.com          # seed (idempotent)
python scripts/seed_demo.py --email demo@example.com --dry-run
python scripts/reset_demo.py --email demo@example.com
```

The seeder ([`src/aegisforge/demo/seed.py`](../src/aegisforge/demo/seed.py))
calls the same ingestion service the REST upload route calls
([`src/aegisforge/services/document_service.py`](../src/aegisforge/services/document_service.py)):
extract → normalise → chunk → embed → persist → index. There is exactly one
ingestion pipeline in the product, and the demo uses it.

Properties:

- **Deterministic** — document ids are `demo-<owner-tag>-<slug>`; the configured
  embedding provider is deterministic, so chunk counts, vectors, citations and
  evaluation scores repeat exactly.
- **Idempotent** — a document already present for the same organization + owner
  with the same content hash is skipped. Drift (changed corpus file) is detected
  by hash and re-ingested under the same id.
- **Tenant-preserving** — every row (document, chunk, embedding) carries the
  seeding user's organization *and* owner id. Retrieval is org + owner scoped
  and fail-closed, so demo knowledge is invisible to any other account.
- **Reported** — the seeder prints an exact created / updated / unchanged
  breakdown and exits non-zero if vector indexing failed.

## 5. Running the demo

1. `docker compose up -d` (API, worker-1, worker-2, PostgreSQL/pgvector, Redis)
2. `cd frontend && npm run dev -- -p 3131`
3. Register an account, then seed it:
   `python scripts/seed_demo.py --email <your email>`
4. Sign in → **Execute** → click the highlighted **Flagship demo** example →
   **Submit & Execute**
5. Watch the Execution Workspace.

## 6. What the workspace shows

| Element | Source | Verified |
|---|---|---|
| Task graph | `GET /workflows/{id}` + `GET /workflows/{id}/tasks` | 4 nodes / 3 waves; dependency edges rendered dynamically from the real graph |
| Dependency edges | plan dependencies | research ∥ rag → analysis → synthesis (3 edges) |
| Task status, duration, retries | `GET /workflows/{id}/tasks` | 4/4 completed in the observed run |
| Tool activity + risk + approval | `GET /tools`, `GET /mcp/tools` | `knowledge.search` recorded on the research task |
| Evidence | `GET /workflows/{id}/result` | 9 cited evidence items in the observed run |
| **Workflow** evaluation grid | `GET /workflows/{id}/evaluations` | observed: planning 1.00 · collaboration 0.85 · final response 1.00 · overall 0.95 |
| Final report | `GET /workflows/{id}/result` | 9 citations, conflicts section, sources, evidence quality; synthesis confidence 0.24 |
| Timeline | task records | request → 4 tasks → evaluation → result |

No chain-of-thought is exposed anywhere: only task summaries, evidence metadata,
citations, and measured scores.

## 7. Result shape

The final report is assembled from measured run data only. The example below is an
**observed value from one seeded demo run**, not an architectural guarantee — exact
chunk counts, citation text, relevance scores, and confidence change with the corpus,
the embedding provider, and the retrieval threshold.

```
[research]  <tool-mediated finding>
[rag]       <retrieval summary>
[analysis]  <evidence counts>

--- Evidence Assessment ---
Evidence quality: 0 strong, 6 moderate, 0 weak, 3 conflicting (overall: 0.27)

## Key findings                     ← each attributed to its source document
## Conflicts detected between sources
## Sources                           ← ranked, with measured relevance

Evidence quality 0.27 · items 9 (strong 0, moderate 6, weak 0, conflicting 3)
```

Sections are omitted when the run produced no evidence for them.

The result’s **synthesis confidence** is conservative by design. In the observed run
it was **0.24**, computed from evidence quality and completeness. That is a
*synthesis* field on `GET /workflows/{id}/result`; it is separate from the
**workflow-level evaluation** on `GET /workflows/{id}/evaluations`, which scored
**planning 1.00 · collaboration 0.85 · final response 1.00 · overall 0.95** for the
same run. A run over thin evidence reports low synthesis confidence rather than
sounding certain — that is intended behaviour, not a defect.

## 8. Tools and MCP

The research leg executes a **real** tool through the registry
(`knowledge.search`), including the permission check, health tracking, circuit
breaker, and tool-call recording. MCP is not configured in the local dev stack,
so the Tools page shows the MCP empty state live; the MCP lifecycle, catalog,
permission, risk and approval paths are covered by their own test suites. No
MCP server is fabricated for the demo.

## 9. Approvals

The flagship decision performs no action, so its plan is low risk and the happy
path does not gate. Approval is a separately validated capability (high-risk
tool action → `action_requires_approval` → manager/admin decision → resume from
the durable checkpoint) with its own live validation and test coverage.

## 10. Tests

`tests/test_demo_flagship.py` (26 tests) asserts behaviour, not demo strings:

- corpus integrity, category coverage, and the presence of real contradictions;
- seeding: creation, idempotency, drift re-ingestion, owner-scoped ids,
  reset-spares-non-demo-documents, two owners in one organization;
- retrieval: cross-document hits, owner/org scoping, fail-closed on missing
  scope, vendor and requirement sides both reachable;
- planning: exact agent sequence, parallel entry, dependency wiring, low risk;
- engine: terminal status, evidence propagation, citations traceable to
  retrieved chunk ids, tool-call recording, evaluation domains, repeatability,
  and cross-tenant non-disclosure;
- full HTTP path: graph, tasks, result, evaluation, owner isolation, history.

Regression tests were also added for the capability changes this phase made:
per-source evidence expansion (`test_phase5_multi_agent.py`), retrieval breadth
(`test_agents.py`), the structured report (`test_phase5_3_intelligence.py`),
and the rate-limit window (`test_rate_limit.py`).

## 11. Live validation performed

Real Chrome via CDP (`frontend/.qa/flagship.mjs`), real backend, real worker:

register → seed (9 created, then 9 unchanged on re-seed) → knowledge page shows
the corpus → flagship example → submit → worker executes 4/4 tasks →
workspace (4 nodes, 3 waves, timeline, evaluation, 9 citations, report with
findings/conflicts/sources) → task detail → refresh recovery → **0 API calls in
9 s after terminal** → history → dashboard (1 request, 100 % success) →
**owner isolation: another account gets 404 on result and by-request** →
repeated run completes with identical quality → 0 console errors, 0 exceptions.

Adversarial checks in the same run: refresh mid/post-execution, repeated
execution, cross-account access. Failure-path behaviour is covered by the
platform's existing failure-injection suites and by the workspace's error
states.

## 12. Known limitations

- **No live external research.** The research leg queries approved internal
  knowledge sources only. Vendor material is ingested as documents, so vendor
  claims are retrieved as evidence with their provenance. This is documented
  rather than simulated: no web scraping, no fabricated "external research".
- **Conflict detection is stance-based.** Conflicts are polarity conflicts
  ("required" vs "not supported"), not semantic or numeric reasoning. The demo
  corpus is written so real conflicts exist; the detector reports them, but it
  will not catch every kind of disagreement.
- **Evaluation is process-local.** Workflow evaluation is computed in the worker
  and persisted in the durable checkpoint, so it survives restarts for a run; it
  is not exposed by the aggregate in-process `/evaluation/metrics` endpoint.
- **Deterministic embeddings.** The default local embedding provider is a
  bag-of-words hash, so similarity is lexical. A configured provider
  (`EMBEDDING_PROVIDER=openai`) changes ranking quality, not the architecture.
- **Confidence is conservative and is a synthesis field, not the workflow score.**
  With mixed-quality evidence the demo reports synthesis confidence around **0.24** on
  `GET /workflows/{id}/result`. The **workflow evaluation** on
  `GET /workflows/{id}/evaluations` is a different number (observed: 0.95 overall;
  planning 1.00, collaboration 0.85, final response 1.00) and is what the workspace
  eval grid shows. Low synthesis confidence with high workflow evaluation is expected,
  not a defect.
- **MCP is unconfigured locally**, so the demo shows the empty state.

## 13. Files

| Path | Purpose |
|---|---|
| `demo/data/**` | Synthetic corpus (9 documents) |
| `demo/README.md` | Dataset, conflicts, seed/reset/run |
| `src/aegisforge/demo/scenario.py` | Canonical request, manifest, deterministic ids |
| `src/aegisforge/demo/seed.py` | Idempotent, owner-scoped seeding + reset |
| `src/aegisforge/services/document_service.py` | Shared ingestion pipeline (route + seeder) |
| `scripts/seed_demo.py`, `scripts/reset_demo.py` | CLIs |
| `tests/test_demo_flagship.py` | Demo behaviour tests |
| `frontend/.qa/flagship.mjs` | Live browser E2E for the demo |