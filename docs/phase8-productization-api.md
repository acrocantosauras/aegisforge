# Phase 8 — Productization API & Operator Surfaces

Phase 7 left a hardened, observable engine. Phase 8 turns it into a
**usable product**: the read APIs the UI needs to show real outcomes, and
honest operator surfaces that report measured state. No execution
machinery was replaced; every new value shown is read from durable state
or probed live — nothing is fabricated.

## Backend API extensions

### `GET /api/v1/workflows/{workflow_id}/result`

The final synthesized outcome for a workflow, exposed as product data:

- `answer`, `summary`, `citations`, `evidence`, `tools_used`, `confidence`,
  `failed_upstream`, `errors`, `agent_type`, `status`.
- Reads the **same durable checkpoint the resume path uses** — what the
  user sees is exactly what recovery would restore.
- Prefers the explicit `agent_result`; falls back to the best terminal
  task record. Tool usage is aggregated across **all** task records
  (upstream research tasks call the tools; the final synthesis task
  rarely does).
- Citations come from the result payload, falling back to evidence
  entries that carry a measured `source`. No chain-of-thought and no
  secret material is ever serialized.

**Isolation**: organization check *and* underlying-request ownership
(the request must have been created by the caller). Registration
currently places users in shared organizations, so an org check alone
would leak execution state between users. Missing/forbidden → **404,
never 403** (no resource-existence oracle).

### `GET /api/v1/workflows/by-request/{request_id}`

Resolves the latest workflow id + status for a request so the frontend
can navigate request → execution workspace without holding workflow ids
in client state. Same tenant isolation, 404 when no workflow exists.

### Owner scoping on workflow introspection

`_assert_request_owner` is now applied to the workflow **tasks**,
**evaluations**, and **graph overview** endpoints: org check plus a
creator check against `requests.requested_by`, 404 on mismatch. This
closes the shared-organization read leak for execution internals.

### `RequestRead.created_at`

The request list now returns creation timestamps — the Execution
History view orders and displays them; guarded by
`TestRequestHistoryField`.

### `GET /api/v1/system`

Real operational status for dashboards (authenticated):

- **database / redis**: `_check_database()` / `_check_redis()`, factored
  out of the readiness probe so `/ready` and `/system` share one
  implementation (readiness keeps its 5s cache).
- **workers**: read from the Redis heartbeat sorted set — worker id,
  heartbeat age, healthy/stale per the 120s TTL.
- **queue depth**: `LLEN aegisforge:jobs`.
- Overall `healthy` only when DB + Redis + at least one worker are up;
  anything less is reported as `degraded` **honestly** — a degraded
  dependency is never swallowed.

## Frontend surfaces

- **API client** (`frontend/src/lib/api.ts`): `getRequestHistory`,
  `executeRequestAsync`, `getWorkflowByRequest`, `getWorkflowTasks`,
  `getWorkflowGraph`, `getWorkflowEvaluation`, `getWorkflowResult`,
  `systemStatus`.
- **Execution workspace** (`/requests/[id]`): now renders the *Final
  Result* — answer, citations, evidence, tools, confidence — alongside
  pipeline, task graph, and evaluation, resolving the workflow through
  `by-request` and polling for async progress.
- **Execution History** (`/history`): request list with `created_at`,
  status filter badges, empty state, row click → workspace.
- **System Health** (`/system`): component cards (API, database, Redis,
  workers, queue depth), worker registry with heartbeat ages, overall
  HEALTHY/DEGRADED badge, 5s auto-refresh plus manual refresh. Load
  failures surface as an explicit error — the page never invents status.
- **New request submit path**: enqueue to the distributed worker queue
  first (`POST /execution/requests/{id}/execute-async` — the real
  production path), falling back to synchronous execution when the
  queue is unavailable.
- **Sidebar**: links for Execution History and System Health.

## CI

- `npm run build` (frontend production build) added to the CI frontend
  job — type-check and unit tests alone do not catch Next.js build
  failures.

## Tests (every guarantee is regression-tested)

| Surface | Tests |
|---|---|
| `tests/test_phase8_product_api.py` — result endpoint (fields, tenant isolation, auth, 404) | 4 |
| … lookup endpoint (resolves, 404, tenant isolation) | 3 |
| … introspection ownership (tasks/evaluations/graph, owner vs stranger) | 6 |
| … request `created_at` | 1 |
| … system status (real components, degrades without Redis) | 2 |
| `frontend/src/app/history/page.test.tsx` | 4 |
| `frontend/src/app/system/page.test.tsx` (cards, registry, empty state, degraded badge, refresh, error) | 6 |

## Validation

| Check | Result |
|---|---|
| `pytest -q` (no local Redis) | **674 passed**, 120 skipped (Redis suites skip by design) |
| `ruff check .` | clean |
| `mypy src` | clean (82 files) |
| Frontend `tsc --noEmit` / `next lint` / vitest | clean, **50 tests passed** |

## Known limitations (documented, not silently accepted)

- `by-request` resolves the **latest** workflow for a request; earlier
  re-runs are not exposed.
- `/system` measures the shared Redis/DB from the API process — it does
  not distinguish per-container health; the `/workers` registry remains
  the authority for individual worker liveness.
- Tool/circuit health stays process-local (Phase 6G non-guarantee);
  `/system` reports the heartbeat view, not breaker state.
