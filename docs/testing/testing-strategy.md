# Testing Strategy

## Principles

- test real behavior, not mock-only behavior
- prefer contract and integration tests over synthetic assertions
- validate security boundaries around tools and permissions
- keep tests small, readable, and deterministic
- do not make the entire test suite dependent on external LLM APIs

## Test Layers

### Unit Tests

#### Domain Models (`test_domain_models.py`)
- Organization and User contract validation
- Request status transitions and permission metadata
- Schema serialization/deserialization

#### Agent Abstraction (`test_agents.py`)
- Base agent permission checking
- Base agent exception handling (RuntimeError → FAILED, PermissionError → DENIED)
- Research agent successful execution with tool calls
- Research agent empty query handling
- Research agent unavailable tool handling
- Research agent permission denial
- Planner generates research plans for research intents
- Planner generates fallback plans for generic intents
- Planner rejects empty intents
- Planner output structure validation

#### Tool Abstraction (`test_tools.py`)
- Tool definition defaults
- Tool execution returns completed status with output
- Tool permission denied when permissions missing
- Tool permission check skipped when None
- Tool exception handling (returns failed status)
- Knowledge tool search by topic
- Knowledge tool empty query handling
- Knowledge tool no-match fallback
- Tool registry register/discover
- Tool registry duplicate prevention
- Tool registry execution with permissions
- Tool registry execution without permissions
- Tool registry execution of nonexistent tool
- Tool registry permission validation

#### Evaluation (`test_workflow.py`)
- Evaluation passes on good results
- Evaluation fails on empty results
- Evaluation fails on failed status
- Evaluation retryable on timeout
- Evaluation fails on missing required fields
- Evaluation flags tool call failures

### Workflow Tests (`test_workflow.py`)

#### Node Tests
- Validate request: valid input → planning status
- Validate request: missing request_id → failed
- Validate request: missing intent → failed
- Plan node creates execution plan
- Execute agent node produces result
- Execute agent node handles empty task list
- Evaluate node with result → evaluating status
- Evaluate node without result → failed evaluation

#### Retry Logic Tests
- Passed evaluation advances to next task
- Passed evaluation on final task → completed
- Retry below limit increments retry count
- Retry at limit → terminal failure

#### Full Workflow Integration
- Complete workflow with research intent → completed status
- Complete workflow with generic intent → completed status

### API Tests (`test_api_auth_and_requests.py`)

#### Auth Flow
- User registration → 201
- User login → 200 with access token
- Create request with auth → 201
- Get request detail → 200

### End-to-End Acceptance Tests (`test_e2e_acceptance.py`)

#### Full Execution Pipeline
- Register user → Login → Create request → Execute workflow → Verify completion
- Verify: execution result has status, workflow_id, final_result
- Verify: final result contains research answer and source
- Verify: tool calls recorded
- Verify: request status updated to completed
- Verify: audit events recorded (workflow.started, workflow.completed)

#### API Contract Validation
- Execute without auth → 401/403
- Execute non-existent request → 404

## Phase 3 Test Additions

### RAG Tests (`test_rag.py`)
- Deterministic embedding provider: vectors, determinism, normalization
- Text extraction: plain text, markdown, unsupported types
- Text normalization: whitespace, control characters
- Chunking: basic, metadata preservation, empty, position tracking
- Ingestion pipeline: plaintext, markdown, empty, large file rejection
- Vector store: add/search, tenant isolation, delete, count
- Cosine similarity: identical, orthogonal, different lengths
- Retrieval service: basic search, tenant isolation, empty query, context building
- RAG agent: retrieval, empty query, no service, insufficient context, citations

### LLM Planner Tests (`test_llm_planner.py`)
- Plan validation: valid, empty tasks, missing plan_id, duplicate IDs, circular deps, invalid agent type, missing description
- Depth computation: linear, flat, empty
- LLM planner: deterministic provider, fallback on no provider, empty intent, structure validation
- Fallback with custom provider that returns unparseable output

### MCP Tests (`test_mcp.py`)
- Mock client: connect, disconnect, discover, invoke, default response
- Tool adapter: creation, execute, permission denied, server not connected, timeout, malformed response
- Tool manager: configure, required server_id, connect and discover, allowed tools filter, disabled server, registry integration

### Async Tests (`test_async_execution.py`)
- Queue: enqueue/dequeue, empty dequeue, requeue
- Job manager: submit, get, update status, idempotency, should_retry, no retry on permission error, no retry at limit, requeue, cancel, list
- Worker: successful job, failing job, no jobs, process all, retries on failure

### Approval Tests (`test_approval.py`)
- Service: create, get, list pending, approve, reject, cannot approve twice, cancel, expiry, not found
- Risk levels: high, critical, low, medium, custom levels
- Safe actions: service restart, config change, workflow operation, unknown
- Cross-user isolation

### Security Tests (`test_security.py`)
- Document upload: valid, too large, empty, path traversal, dangerous extension, null bytes, disallowed type
- Filename sanitization: normal, path traversal, special chars, long, empty
- Tenant isolation: same org, cross org, missing org
- Prompt injection: ignore instructions, system prompt, act as, pretend, override, clean, empty, inst token
- Secret detection: API key, password, bearer token, no secrets
- Content type: allowed, disallowed, custom
- Model output: valid, empty, too long, JSON valid/invalid

### Observability Tests (`test_observability.py`)
- Tracer: spans, multiple spans, exceptions, summary, total duration, get by name
- Metrics collector: create, get, list, summary, empty summary, global singleton
- Execution metrics: to_dict

### Evaluation Tests (`test_evaluation.py`)
- RAG evaluation: with results, insufficient context, empty
- Plan evaluation: valid, with errors, empty
- Execution evaluation: all passed, with failures, empty, with retries, tool failure

### E2E Phase 3 Tests (`test_e2e_phase3.py`)
- Scenario A: Full RAG pipeline with document ingestion, embedding, storage, retrieval, and evaluation
- Scenario B: MCP tool integration with discovery, permission check, and execution
- Scenario C: Human approval with risk assessment, approval, rejection, and safe action
- Full pipeline: RAG + Async execution + Worker processing

## Test Configuration

- SQLite in-memory database for test isolation
- `StaticPool` for SQLite in-memory to ensure shared connection
- `get_engine()` cached with `lru_cache` for engine reuse
- `app.dependency_overrides[get_db]` for database override in tests
- No external service dependencies (LLM, Redis, network)
- Deterministic fake providers for embeddings and LLM
- Mock MCP client for tool integration testing
- In-memory job queue for async execution testing
- In-memory checkpoint store for workflow checkpointing testing
- 382 backend tests pass; 23 real-infrastructure integration tests run opt-in via `AEGISFORGE_INTEGRATION_TESTS=true`; 35 frontend tests via `npm test`

## Phase 4 Test Additions

### Checkpoint Tests (`test_checkpoint.py`)
- Checkpoint store: save, load, load latest, list, delete by workflow
- Workflow checkpointer: save and resume, get current node, multi-workflow isolation, clear, execution ID uniqueness
- State sanitization: sensitive key removal, safe key preservation
- Approval integration: risk level checks, approval lifecycle, rejection, expiry, not found

### Security Phase 4 Tests (`test_security_phase4.py`)
- Document security: oversized file, disallowed type, valid upload, path traversal, special chars, empty filename, long filename
- Prompt injection: checkpoint secret removal, workflow data preservation, untrusted document not executed
- Tenant isolation: document ownership, chunk ownership, job ownership
- Secret handling: safe config defaults, trace context filtering

### E2E Phase 4 Tests (`test_e2e_phase4.py`)
- Scenario A: Full RAG pipeline (ingest → embed → store → retrieve → context) with tenant isolation
- Scenario B: Async workflow (submit → queue → worker → result) with retry, idempotency, cancellation
- Scenario C: Human approval (create → approve → resume) and rejection
- Scenario D: Failure recovery (checkpoint → failure → resume) and worker crash recovery

## Running Tests

```bash
# All backend tests (382 passed; integration tests skipped unless enabled)
python -m pytest tests/ -v

# Specific test file
python -m pytest tests/test_tools.py -v

# With coverage
python -m pytest tests/ --cov=aegisforge --cov-report=term-missing
```

## Phase 5 Test Additions

### Multi-Agent Execution (`test_phase5_multi_agent.py`)
- dependency-aware DAG validation and execution waves
- bounded parallel execution and dependency ordering
- inter-agent evidence and structured output passing
- retries, timeouts, partial failure policies, and approval pause/resume
- checkpoint snapshots and late-timeout result protection

### Production-Path Coverage (`test_phase5_e2e.py`)
- authenticated API request through LangGraph and the multi-agent scheduler
- Research/RAG parallel tasks followed by Analysis and Synthesis
- persisted task introspection and workflow-level evaluation
- production async submission fails closed when Redis is unavailable

The Phase 5 application-path test uses deterministic providers and SQLite for
repeatability. The live API -> Redis -> worker -> PostgreSQL test was also
validated with the Docker services running; it completed through LangGraph,
Research, Analysis, Synthesis, Evaluation, and checkpoint-backed introspection.
