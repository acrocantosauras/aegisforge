# AegisForge — Phase 4 Production Readiness Audit

**Date:** September 3, 2026
**Auditor:** Buffy (Codebuff)
**Scope:** Full verification of Phase 4 claims against actual implementation

---

## 1. Executive Summary

Phase 4 reported completion with 279 passing tests, real LLM integration, PostgreSQL + pgvector, Redis async execution, LangGraph checkpointing, human approval, observability, frontend, and Docker.

**This audit found that the majority of Phase 4 components exist as well-structured abstractions but are NOT wired into the actual execution path.** The production workflow still uses the deterministic planner, the ResearchAgent (not the RAG agent), the deterministic evaluator (not the LLM critic), and a synchronous execution model. Prometheus metrics are defined but never instrumented. Approvals are in-memory only. The worker silently falls back to an in-memory queue if Redis is unavailable. The Alembic migration uses a hardcoded SQLite URL.

**Overall Conclusion: NOT PRODUCTION READY**

The codebase has strong architectural foundations and good abstractions, but the critical execution path has multiple disconnections between what exists and what is actually used.

---

## 2. Phase 4 Claims Reviewed

| Claim | Verified? | Notes |
|-------|-----------|-------|
| Real LLM provider integration | ❌ **NOT WIRED** | Providers exist but are not used in the workflow |
| PostgreSQL + pgvector | ⚠️ **PARTIAL** | Migration exists; Alembic uses SQLite URL; in-memory vector store used in tests |
| Redis async execution | ⚠️ **PARTIAL** | Queue exists; API endpoint is synchronous, never uses Redis |
| LangGraph checkpointing | ⚠️ **PARTIAL** | Checkpointer exists; not passed by worker or execution service |
| Human approval pause/resume | ⚠️ **PARTIAL** | Approval service exists; in-memory only, not DB-backed |
| OpenTelemetry | ⚠️ **PARTIAL** | OTel collector config exists; no instrumentation in code |
| Prometheus | ❌ **NOT INSTRUMENTED** | Metrics defined; tracking functions never called |
| Grafana | ⚠️ **PARTIAL** | Dashboard JSON exists with correct metric names; metrics never populated |
| LLM evaluation/critic | ❌ **NOT WIRED** | Critic class exists; never called from workflow |
| React/Next.js frontend | ✅ **FUNCTIONAL** | Screens exist with API integration |
| Docker Compose | ⚠️ **PARTIAL** | Services defined; Alembic URL issue; worker falls back to in-memory |
| Alembic migrations | ⚠️ **PARTIAL** | Migration exists; hardcoded SQLite URL in alembic.ini |
| Security hardening | ⚠️ **PARTIAL** | Validation exists; CORS wide open; no CSRF |
| 279 tests passing | ✅ **TRUE** | But many are unit-level with in-memory fakes, not true E2E |
| E2E scenarios | ⚠️ **MISLEADING** | Tests use InMemoryVectorStore, InMemoryJobQueue, DeterministicEmbeddingProvider — not real infrastructure |

---

## 3. Architecture Assessment

**Rating: GREEN**

The modular monolith architecture is sound. The package structure (agents, tools, workflows, rag, llm, mcp, approval, evaluation, observability, security) provides clean separation. Dependencies flow in one direction. Domain logic is independent of AI frameworks.

The `BaseAgent → BaseTool → ToolRegistry → PermissionChain → ExecutionPlan → LangGraph → Evaluation → Audit` pipeline is well-designed.

**Issue:** The architecture is well-designed but the wiring between components is incomplete. Many components exist in parallel but aren't connected in the actual execution path.

---

## 4. Real LLM Assessment

**Rating: RED**

### What exists:
- `ModelProvider` abstract class with `OpenAIModelProvider`, `AnthropicModelProvider`, `DeterministicModelProvider`
- Retry with exponential backoff, timeout handling, structured output validation
- API key from environment variables only
- `LLMPlannerAgent` with plan validation and deterministic fallback
- `LLMCritic` for RAG, plan, and response evaluation
- `RAGAgent` for retrieval-augmented generation

### What is actually used in production:
- `plan_node()` in `langgraph_workflow.py` line 104: `planner = PlannerAgent()` — uses the **deterministic rule-based planner**, NOT `LLMPlannerAgent()`
- `execute_agent_node()` line 128: `agent = ResearchAgent(registry=registry)` — only uses `ResearchAgent`, NEVER uses `RAGAgent`
- `evaluate_node()` line 162: `evaluator = ResultEvaluator()` — uses the **deterministic evaluator**, NOT `LLMCritic`

### Evidence:
```
# langgraph_workflow.py:104
def plan_node(state):
    context = _make_context(state)
    planner = PlannerAgent()  # <-- DETERMINISTIC, not LLMPlannerAgent
    result = planner.execute(...)
```

```
# langgraph_workflow.py:128
def execute_agent_node(state):
    registry = _build_registry()
    agent = ResearchAgent(registry=registry)  # <-- Only ResearchAgent
    # RAGAgent is never instantiated or used
```

```
# langgraph_workflow.py:162
def evaluate_node(state):
    evaluator = ResultEvaluator()  # <-- Deterministic only
    evaluation = evaluator.evaluate(...)
    # LLMCritic is never instantiated or used
```

### Impact:
The entire LLM integration claim is false for the production execution path. Configuring `LLM_PROVIDER=openai` and `LLM_API_KEY=...` would have **no effect** on workflow execution. The system would still use the deterministic planner, ResearchAgent, and deterministic evaluator.

---

## 5. PostgreSQL + pgvector Assessment

**Rating: YELLOW**

### What exists:
- SQLAlchemy models for documents, chunks, execution_jobs, approval_requests
- Alembic migration `20260903_phase3_rag_mcp_async_approval.py` creating all tables
- `PgVectorStore` class with pgvector operations
- Docker Compose with `pgvector/pgvector:pg16` image
- Document upload API persisting to PostgreSQL

### Issues:

1. **Alembic URL is hardcoded to SQLite:**
```
# alembic.ini
sqlalchemy.url = sqlite:///./aegisforge.db
```
The Docker Compose sets `DATABASE_URL` environment variable, but `alembic/env.py` reads from `config.get_main_option("sqlalchemy.url")` which comes from `alembic.ini`, NOT from the environment. Running `alembic upgrade head` in Docker would migrate SQLite, not PostgreSQL.

2. **In-memory vector store in document upload:**
```python
# documents.py
vector_store = get_vector_store(
    "pgvector" if settings.database_url.startswith("postgresql") else "memory",
    ...
)
```
On SQLite (default), documents would be ingested but vectors stored in memory only — lost on restart.

3. **PgVector embedding dimension is hardcoded to 384** in the migration, matching the default config. If a user configures a different embedding dimension, the migration would create mismatched columns.

4. **No vector index creation in migration** for ivfflat or hnsw — the migration creates the table but only creates a basic org_id index, not a vector similarity index.

---

## 6. Redis + Worker Assessment

**Rating: RED**

### What exists:
- `RedisJobQueue` and `InMemoryJobQueue` classes
- `JobManager` with submit, retry, idempotency, cancellation
- `JobWorker` with process_next_job and process_all
- Worker process with signal handlers and graceful shutdown
- Docker Compose with Redis service

### Critical Issues:

1. **API endpoint is synchronous — never uses Redis:**
```python
# execution.py
@router.post("/execution/requests/{request_id}/execute")
def execute_request_route(request_id, db, user):
    result = execute_request(...)  # Synchronous! Blocks until complete
    return result
```
The `POST /execution/requests/{id}/execute` endpoint calls `execute_request()` which runs the entire LangGraph workflow synchronously within the HTTP request. There is **no code anywhere in the API routes** that calls `JobManager.submit_job()` or interacts with Redis.

2. **Worker falls back to in-memory queue on Redis failure:**
```python
# worker.py
try:
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    redis_client.ping()
    queue = RedisJobQueue(redis_client)
except Exception:
    queue = InMemoryJobQueue()  # <-- Jobs lost on restart!
```
If Redis is unavailable, the worker silently uses an in-memory queue. All jobs would be lost on worker restart. There is no warning to the user that their jobs won't persist.

3. **Worker does not use the async execution path from the API.** The worker creates its own `execute_workflow()` call directly, not through the `ExecutionJobModel` database model.

---

## 7. LangGraph Checkpointing Assessment

**Rating: YELLOW**

### What exists:
- `InMemoryCheckpointStore` with save, load, list, delete
- `WorkflowCheckpointer` with save_after_node, load_resume_state
- State sanitization removing API keys, tokens, passwords
- Checkpointing integrated into the LangGraph workflow nodes

### Issues:

1. **Checkpointer is never passed to execute_workflow:**
```python
# execution_service.py
final_state = execute_workflow(
    request_id=request_id,
    intent=request.intent,
    user_id=user_id,
    organization_id=organization_id,
    workflow_id=workflow_id,
)
# No checkpointer parameter! Checkpointing is disabled.
```

```python
# worker.py
final_state = execute_workflow(
    request_id=job.request_id,
    intent=request.intent,
    ...
)
# No checkpointer parameter! Checkpointing is disabled.
```

2. **In-memory store only:** Checkpoints are lost on process restart. The scenario "workflow starts → process terminates → process restarts → approval completed → workflow resumes" **cannot work** because checkpoints exist only in process memory.

3. **The resume functionality exists** (`resume_workflow_after_approval()`) but has no caller — no code path invokes it.

---

## 8. Human Approval Assessment

**Rating: RED**

### What exists:
- `ApprovalService` with create, approve, reject, cancel, expiry
- Risk level classification (LOW, MEDIUM, HIGH, CRITICAL)
- `ApprovalRequestModel` in database with full schema
- API endpoints for list, approve, reject with role-based authorization
- Workflow `retry_or_complete_node` checks approval requirements

### Critical Issues:

1. **Approvals are in-memory only:**
```python
# approvals.py
_approval_service = ApprovalService()  # Module-level in-memory instance
```
The API routes use a module-level `ApprovalService()` instance. Approvals are stored in a Python dict, not in the database. Restarting the API server loses all pending approvals. Despite `ApprovalRequestModel` existing in the database, it is **never used** by the approval API routes.

2. **No workflow integration for approval pause/resume:**
The workflow `retry_or_complete_node` sets `status=WAITING_FOR_APPROVAL` when approval is required, but:
- No code creates an approval request when this status is set
- No code resumes the workflow when an approval decision is made
- The approval API and the workflow are disconnected

3. **No tenant isolation on approval lookup:**
```python
def get_pending_approvals(self, organization_id=""):
    for approval in self._approvals.values():
        if approval.status != ApprovalStatus.PENDING:
            continue
        if organization_id and approval.workflow_id and not approval.workflow_id.startswith(organization_id):
            continue  # This check is incorrect — workflow_id doesn't start with org_id
```
The filtering logic checks if `workflow_id` starts with `organization_id`, which is incorrect — workflow IDs are formatted as `wf-<random>`, not `<org>-<random>`.

---

## 9. RAG Assessment

**Rating: YELLOW**

### What exists:
- Modular ingestion pipeline (extraction → normalization → chunking → embedding → storage)
- Configurable chunking with metadata
- Pluggable embedding providers (Deterministic, OpenAI)
- Vector store abstraction (InMemory, PgVector)
- RetrievalService with semantic search, tenant filtering, similarity thresholds
- RAGAgent with citations and insufficient-context handling

### Issues:

1. **RAGAgent is never used in the workflow.** The `execute_agent_node` only instantiates `ResearchAgent`. To use RAG, the workflow would need to select the RAGAgent based on the plan's `assigned_agent_type`, but the current code hardcodes `ResearchAgent`.

2. **RAG generation does not use LLM.** Even if RAGAgent were wired in, it only retrieves and formats text — it does not call an LLM to generate a grounded answer. The "grounded answer" is just concatenated retrieval results.

3. **Vector storage silently fails:**
```python
# documents.py
try:
    vector_store.add(entries, organization_id=user.organization_id)
except Exception as exc:
    logger.warning("Vector storage failed (non-fatal): %s", exc)
```
Documents are ingested and chunks are persisted to PostgreSQL, but if vector storage fails, the user gets a success response with no indication that semantic search won't work for their document.

---

## 10. Evaluation Assessment

**Rating: RED**

### What exists:
- `ResultEvaluator` with structural validation (status, fields, tool calls)
- `LLMCritic` with RAG, plan, and response evaluation
- `RAGEvaluator`, `PlanEvaluator`, `ExecutionEvaluator`
- Combined evaluation concept

### Issues:

1. **LLM critic is never called.** The workflow `evaluate_node` uses `ResultEvaluator()` only. The `LLMCritic` class exists but has zero callers in the production code path.

2. **RAG evaluator disconnected from workflow:**
The `evaluate_rag_result()` function in `evaluation/evaluators.py` exists but is never called from the workflow or any API endpoint.

3. **No evaluation persistence.** Evaluation results are stored in the workflow state dict but never persisted to the database.

---

## 11. Observability Assessment

**Rating: RED**

### What exists:
- Prometheus metric definitions (Counter, Histogram, Gauge) for HTTP, LLM, workflow, tool, RAG, approval, queue
- Tracking context managers: `track_http_request`, `track_llm_request`, `track_workflow`, `track_tool_execution`, `track_rag_retrieval`
- OTel collector configuration
- Grafana dashboard JSON with correct metric names

### Critical Issues:

1. **Tracking functions are NEVER called:**
```
# Code search for track_http_request, track_llm_request, etc. in src/aegisforge/
# Result: 5 matches — ALL are function definitions in metrics.py
# Zero calls from app.py, workflow, agents, tools, or any other module
```
The Prometheus metrics will always show zero values. The Grafana dashboards will show empty graphs. The `/metrics` endpoint will return metric definitions with no data.

2. **No HTTP middleware for request tracking:**
The FastAPI app has a `add_request_id` middleware but no middleware that calls `track_http_request()`.

3. **No OpenTelemetry instrumentation:**
The OTel collector is configured in Docker Compose but no Python code imports or uses OpenTelemetry SDK.

---

## 12. Frontend Assessment

**Rating: GREEN**

### What exists:
- Next.js 14 with App Router, React 18, TypeScript
- JWT authentication (login/register) via `AuthProvider`
- Dashboard with stats, requests, approvals
- Create Request page with task submission
- Workflow Detail page
- Approval UI with approve/reject
- Document upload and management
- Execution logs with audit events
- API client with all endpoint integration
- CORS proxy via next.config.js

### Strengths:
- Complete UI for all major features
- Proper JWT token management
- Tenant-scoped API calls
- Loading and error states on most pages
- Form validation

### Issues:
- No production CSS/styling (functional but plain)
- No WebSocket for real-time updates
- `any` types used in error handling
- No TypeScript type generation from backend OpenAPI
- No unit tests for frontend components
- Frontend CI only runs lint and type-check, no tests

---

## 13. Security Assessment

**Rating: YELLOW**

### Strengths:
- JWT authentication with proper token validation
- Password hashing with bcrypt
- Document upload validation (size, content type, safe filenames, path traversal)
- Prompt injection detection patterns
- Tenant isolation on document queries (org_id filter)
- Checkpoint state sanitization (removes API keys, tokens, passwords)
- Role-based approval authorization (admin/manager only)
- Audit event recording

### Issues:

1. **CORS is wide open:**
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # <-- Accepts requests from any origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```
With `allow_origins=["*"]` and `allow_credentials=True`, any website can make authenticated requests to the API.

2. **No CSRF protection on frontend.**

3. **No rate limiting on any endpoint.**

4. **Approval tenant isolation is incorrect:**
The `get_pending_approvals` method checks if `workflow_id.startswith(organization_id)`, which doesn't match the actual workflow ID format.

5. **No password complexity beyond minimum length 8.**

6. **Secret key hardcoded in docker-compose.yml:**
```yaml
SECRET_KEY: dev-secret-key-change-in-production
```

7. **Trust client-provided organization_id on document upload:** The document upload endpoint uses `user.organization_id` from the authenticated user, which is correct. However, the vector store metadata uses `user.organization_id` which is good.

8. **No validation that uploaded documents don't contain malicious content beyond prompt injection patterns.**

---

## 14. Docker/Deployment Assessment

**Rating: YELLOW**

### What exists:
- Docker Compose with: PostgreSQL (pgvector), Redis, API, Worker, Frontend, OTel Collector, Prometheus, Grafana
- Health checks on PostgreSQL, Redis, and API
- Dependency ordering with `condition: service_healthy`
- Persistent volumes for PostgreSQL, Redis, Prometheus
- `.env.example` documenting environment variables
- Dockerfile with multi-stage build

### Issues:

1. **Alembic migration URL mismatch:**
The Docker Compose sets `DATABASE_URL` but `alembic.ini` hardcodes `sqlite:///./aegisforge.db`. The `api` service command runs `alembic upgrade head` which would try to migrate SQLite, not PostgreSQL.

2. **Frontend service is behind a profile:**
```yaml
frontend:
    profiles:
      - full
```
The frontend only starts with `docker compose --profile full up`. Default `docker compose up` doesn't include the frontend.

3. **Observability services are behind a profile:**
```yaml
otel-collector:
    profiles:
      - observability
```
Same issue — observability stack requires explicit profile activation.

4. **Worker has no health check.**

5. **`Dockerfile` doesn't copy test files** — not an issue for production, but means integration tests can't run inside the container.

6. **Volume mount `.:/app` overrides the installed package** — this is intentional for development but could cause issues with stale code.

---

## 15. Testing Assessment

**Rating: YELLOW**

### Test Count: 279 tests, all passing

### Classification:

| Type | Count | Description |
|------|-------|-------------|
| Unit | ~200 | Agent, tool, domain model, security validation, evaluator tests |
| Integration | ~50 | API auth, workflow, RAG pipeline, async jobs, approval service |
| E2E | ~29 | Phase 3 and Phase 4 scenario tests |

### Issues:

1. **"E2E" tests are not truly end-to-end:**
- `test_e2e_phase4.py::TestScenarioA` uses `InMemoryVectorStore` and `DeterministicEmbeddingProvider` — NOT real PostgreSQL+pgvector
- `test_e2e_phase4.py::TestScenarioB` uses `InMemoryJobQueue` — NOT real Redis
- `test_e2e_phase4.py::TestScenarioC` uses `ApprovalService()` in-memory — NOT database-backed
- `test_e2e_phase4.py::TestScenarioD` uses `InMemoryCheckpointStore` — NOT durable persistence

2. **No integration tests with real PostgreSQL+pgvector.**

3. **No integration tests with real Redis.**

4. **No integration tests with real LLM providers.**

5. **No frontend tests at all** — no component tests, no E2E browser tests.

6. **The one true API E2E test** (`test_e2e_acceptance.py`) uses SQLite in-memory database and `TestClient` — it tests the API contract but not real infrastructure.

7. **Missing test categories:**
- No cross-tenant API-level isolation tests (e.g., user from org-A calling org-B's document endpoint)
- No concurrent access tests
- No JWT expiry tests
- No document upload security integration tests
- No approval authorization bypass tests

---

## 16. Production Readiness Scorecard

| Area | Rating | Evidence | Risk | Required Action |
|------|--------|----------|------|-----------------|
| Architecture | 🟢 GREEN | Clean modular monolith, well-separated packages | Low | None |
| Real LLM | 🔴 RED | Providers exist but not wired into workflow | Critical | Wire LLMPlannerAgent, RAGAgent, LLMCritic into workflow |
| PostgreSQL | 🟡 YELLOW | Models and migration exist; Alembic URL hardcoded to SQLite | High | Fix alembic.ini to read from env |
| pgvector | 🟡 YELLOW | PgVectorStore exists; InMemoryVectorStore used in most paths | High | Wire PgVectorStore in document ingestion; add vector index |
| Redis | 🔴 RED | Queue classes exist; API never uses async path | Critical | Add async API endpoint using JobManager |
| Worker | 🟡 YELLOW | Worker exists; falls back to in-memory queue silently | High | Remove silent fallback; require Redis |
| LangGraph | 🟢 GREEN | Workflow graph is well-structured | Low | None |
| Checkpointing | 🔴 RED | Checkpointing code exists; never passed to execute_workflow | High | Wire checkpointer into worker and execution service |
| Approval | 🔴 RED | In-memory only; not DB-backed; no workflow integration | Critical | Persist to DB; wire into workflow pause/resume |
| RAG | 🟡 YELLOW | Pipeline works; RAGAgent never used in workflow | High | Wire RAGAgent into workflow based on plan task type |
| Evaluation | 🔴 RED | Deterministic evaluator works; LLM critic never called | High | Wire LLMCritic into evaluate_node |
| Security | 🟡 YELLOW | Good foundations; CORS wide open; no CSRF/rate limiting | Medium | Tighten CORS; add rate limiting |
| Observability | 🔴 RED | Metrics defined; never instrumented | High | Add tracking calls to app, workflow, agents, tools |
| Frontend | 🟢 GREEN | Functional UI with API integration | Low | Add production styling and tests |
| Docker | 🟡 YELLOW | Services defined; Alembic URL issue; profiles hide services | Medium | Fix Alembic URL; make frontend default |
| Testing | 🟡 YELLOW | 279 tests pass; many are in-memory fakes, not real infra | Medium | Add real PostgreSQL/Redis integration tests |
| CI/CD | 🟢 GREEN | Lint, type-check, test for Python and frontend | Low | Add integration test workflow |

---

## 17. Findings

### P0 — Critical

**F1: LLM planner not wired into workflow**
- **File:** `src/aegisforge/workflows/langgraph_workflow.py:104`
- **Evidence:** `planner = PlannerAgent()` instead of `LLMPlannerAgent()`
- **Why it matters:** Configuring an LLM API key has zero effect on planning
- **Impact:** The "LLM-Assisted Planning" claim is false for the production path
- **Fix:** Accept `ModelProvider` in `execute_workflow()`, pass to `LLMPlannerAgent`

**F2: RAG agent never used in workflow**
- **File:** `src/aegisforge/workflows/langgraph_workflow.py:128`
- **Evidence:** `agent = ResearchAgent(registry=registry)` — RAGAgent never instantiated
- **Why it matters:** Document ingestion is pointless if RAG retrieval is never triggered
- **Impact:** The "RAG Agent" claim is false for the production path
- **Fix:** Route tasks to RAGAgent based on `assigned_agent_type` in the plan

**F3: Prometheus metrics never instrumented**
- **File:** `src/aegisforge/observability/metrics.py` (definitions) vs. rest of codebase (zero calls)
- **Evidence:** `track_http_request`, `track_workflow`, `track_tool_execution`, `track_rag_retrieval` defined but never called
- **Why it matters:** All Grafana dashboards show empty graphs; monitoring provides zero value
- **Impact:** The "Observability" claim is false — metrics infrastructure exists but is disconnected
- **Fix:** Add middleware for HTTP tracking; add tracking calls in workflow, agent, tool nodes

**F4: Approvals are in-memory only**
- **File:** `src/aegisforge/api/routes/approvals.py:21`
- **Evidence:** `_approval_service = ApprovalService()` — module-level in-memory dict
- **Why it matters:** Restarting the API loses all pending approvals; `ApprovalRequestModel` in DB is never used
- **Impact:** The "Human Approval" persistence claim is false
- **Fix:** Create DB-backed `ApprovalService` using `ApprovalRequestModel`

**F5: API execution endpoint is synchronous**
- **File:** `src/aegisforge/api/routes/execution.py:32`
- **Evidence:** `execute_request()` runs synchronously; no Redis/worker involvement
- **Why it matters:** Long-running workflows block HTTP connections; no async execution from API
- **Impact:** The "Async Workflow Execution" claim is false for the API path
- **Fix:** Add async endpoint that submits to JobManager/Redis

### P1 — Important

**F6: Alembic migration URL hardcoded to SQLite**
- **File:** `alembic.ini:2`
- **Evidence:** `sqlalchemy.url = sqlite:///./aegisforge.db`
- **Why it matters:** `alembic upgrade head` in Docker migrates SQLite, not PostgreSQL
- **Impact:** Database schema may not match PostgreSQL in production
- **Fix:** Read URL from environment variable in `alembic/env.py`

**F7: Worker silently falls back to in-memory queue**
- **File:** `src/aegisforge/worker.py:83-85`
- **Evidence:** `except Exception: queue = InMemoryJobQueue()`
- **Why it matters:** Jobs are silently lost on worker restart
- **Impact:** Data loss without warning
- **Fix:** Log clear warning; require Redis for production; exit if Redis unavailable

**F8: LLM critic never called from workflow**
- **File:** `src/aegisforge/workflows/langgraph_workflow.py:162`
- **Evidence:** `evaluator = ResultEvaluator()` — LLMCritic never imported or used
- **Why it matters:** The "LLM-based Evaluation/Critic" claim is false for production
- **Impact:** No semantic quality assessment
- **Fix:** Wire LLMCritic into evaluate_node when ModelProvider is available

**F9: Checkpointer never passed to execute_workflow**
- **File:** `src/aegisforge/services/execution_service.py:56` and `src/aegisforge/worker.py:68`
- **Evidence:** `execute_workflow()` called without `checkpointer` parameter
- **Why it matters:** Workflow state is not persisted; process restart loses all progress
- **Impact:** The "checkpointing survives restart" claim is false in production
- **Fix:** Create and pass checkpointer from worker/execution service

**F10: Approval workflow integration missing**
- **File:** `src/aegisforge/workflows/langgraph_workflow.py:230` and `src/aegisforge/api/routes/approvals.py`
- **Evidence:** Workflow sets `WAITING_FOR_APPROVAL` status but no code creates an approval request or resumes the workflow
- **Why it matters:** Approval is a dead-end status — workflow never actually pauses and resumes
- **Impact:** The "approval pauses workflow" claim is false
- **Fix:** Wire approval creation into workflow; wire approval decision into resume

### P2 — Medium

**F11: CORS wide open**
- **File:** `src/aegisforge/app.py:36`
- **Evidence:** `allow_origins=["*"]` with `allow_credentials=True`
- **Why it matters:** Any website can make authenticated requests to the API
- **Fix:** Restrict to frontend origin

**F12: No rate limiting**
- **Impact:** Vulnerable to brute force and DoS
- **Fix:** Add rate limiting middleware

**F13: Vector storage failure is non-fatal**
- **File:** `src/aegisforge/api/routes/documents.py:117`
- **Evidence:** `except Exception: logger.warning("Vector storage failed (non-fatal)")`
- **Why it matters:** User gets success response but document can't be semantically searched
- **Fix:** Return partial success indicator or fail the upload

**F14: Approval tenant isolation is incorrect**
- **File:** `src/aegisforge/approval/service.py:64`
- **Evidence:** `approval.workflow_id.startswith(organization_id)` — workflow IDs don't start with org IDs
- **Why it matters:** Tenant isolation on approvals is broken
- **Fix:** Store organization_id on ApprovalRequest and filter by it

**F15: No frontend tests**
- **Impact:** No regression protection for UI
- **Fix:** Add component tests and E2E browser tests

### P3 — Future

**F16:** No distributed workflow execution
**F17:** No production monitoring/alerting rules
**F18:** No WebSocket for real-time updates
**F19:** No API versioning
**F20:** No TypeScript type generation from OpenAPI

---

## 18. Technical Debt

1. **Disconnected abstractions:** Many well-designed components exist but aren't wired together
2. **In-memory defaults:** ApprovalService, CheckpointStore, JobQueue all default to in-memory
3. **Deterministic-only production path:** The workflow never actually uses LLM or RAG
4. **No integration test infrastructure:** No Docker-based test setup for PostgreSQL/Redis
5. **Docker profiles hide services:** Frontend and observability not started by default
6. **No health checks for worker**
7. **No database connection pooling configuration**
8. **No graceful degradation when LLM unavailable**

---

## 19. Recommended Fixes (Priority Order)

### Must fix before claiming production readiness:

1. **Wire LLMPlannerAgent into workflow** — Accept ModelProvider parameter, use it in plan_node
2. **Wire RAGAgent into workflow** — Route tasks to RAGAgent when agent_type is "rag"
3. **Add async API endpoint** — POST /requests/{id}/execute-async that submits to Redis
4. **Fix Alembic URL** — Read from DATABASE_URL environment variable
5. **Persist approvals to database** — Use ApprovalRequestModel instead of in-memory dict
6. **Wire checkpointer into worker** — Pass WorkflowCheckpointer to execute_workflow
7. **Instrument Prometheus metrics** — Add tracking calls to HTTP middleware, workflow, agents, tools
8. **Fix approval tenant isolation** — Store and filter by organization_id on ApprovalRequest
9. **Remove silent Redis fallback** — Worker should require Redis in production
10. **Tighten CORS** — Restrict to frontend origin

### Should fix soon:

11. Wire LLMCritic into evaluate_node
12. Add rate limiting
13. Fix vector storage silent failure
14. Add real PostgreSQL/Redis integration tests
15. Add frontend tests

---

## 20. What Can Be Safely Deferred

- Frontend production styling
- WebSocket real-time updates
- API versioning
- TypeScript type generation from OpenAPI
- Distributed workflow execution
- Production monitoring/alerting rules
- CSRF protection
- Advanced RAG (hybrid search, re-ranking)
- Multi-agent collaboration
- Kubernetes deployment

---

## 21. Recommendation for Phase 5

Before Phase 5, **Phase 4 must be completed** by wiring the disconnected components. The recommended Phase 5 scope should be:

1. **Complete Phase 4 wiring** (the 10 must-fix items above)
2. **Add integration test infrastructure** (Docker-based tests with real PostgreSQL, Redis, and mock LLM)
3. **Production security hardening** (CORS, rate limiting, CSRF, audit)
4. **Real frontend testing** (component tests, E2E browser tests)
5. **Then** proceed to advanced features (distributed execution, advanced RAG, multi-agent)

---

## Overall Conclusion

```
NOT PRODUCTION READY
```

The Phase 4 codebase has strong architectural foundations and well-designed abstractions. However, the critical execution path has multiple disconnections: the LLM planner, RAG agent, LLM critic, Prometheus metrics, async execution, checkpointing, and approval persistence all exist as code but are not wired into the production execution flow. The system currently runs on deterministic planning, in-memory state, and synchronous execution — which was already the Phase 2/3 capability level.

Phase 4's value is in the abstractions and infrastructure it established. Completing the wiring (estimated 10 focused fixes) would transform this into a genuinely production-capable system.
