# AegisForge

AegisForge is an enterprise-oriented multi-agent AI platform designed for reliable task orchestration, controlled tool use, retrieval from private knowledge, evaluation, and auditable execution.

## Problem Statement

Modern enterprise work often crosses multiple domains at once: research, knowledge retrieval, code analysis, document processing, and decision support. A single LLM interaction is usually not enough. Teams need a system that can plan work, route specialized tasks to the right agents, interact with approved tools, retrieve only authorized information, and produce auditable outputs under enterprise constraints.

## Target Users

- enterprise knowledge workers
- software engineers and engineering teams
- analysts and researchers
- operations and support functions
- compliance and governance stakeholders

## Architecture

AegisForge follows a modular monolith architecture with clear separation between:

- **Control Plane**: users, organizations, permissions, policies, workflow configuration, audit configuration
- **Execution Plane**: requests, planning, agent execution, retrieval, tools, retries, evaluation, results, observability

### Execution Pipeline (Phase 4)

```
Authenticated User
        ↓
Create Request (API)
        ↓
Redis Job Queue
        ↓
Worker Process
        ↓
LangGraph Workflow (with checkpointing)
        ↓
LLM Planner / Deterministic Fallback
        ↓
Structured ExecutionPlan (validated)
        ↓
Agent Execution (RAG / Research / MCP)
        ↓
Permission Check (User → Agent → Tool)
        ↓
Tool / MCP / Knowledge Retrieval
        ↓
Evaluation (Deterministic + LLM Critic)
        ↓
Retry or Approval Pause or Complete
        ↓
PostgreSQL + pgvector persistence
        ↓
OpenTelemetry Traces + Prometheus Metrics
        ↓
Frontend (React/Next.js)
        ↓
Audit Trail
```

## Implemented Capabilities

### Phase 0 — Architecture Foundation
- Modular monolith architecture (ADR 0001)
- PostgreSQL, Redis, Docker Compose
- Alembic migrations, CI pipeline

### Phase 1 — Core Platform
- FastAPI backend, JWT authentication
- Request lifecycle management

### Phase 2 — Agent Runtime
- BaseAgent, BaseTool, ToolRegistry abstractions
- LangGraph workflow with retry logic
- Research Agent, Planner, Evaluation
- Permission enforcement, audit events
- 61 passing tests

### Phase 3 — RAG, LLM Planning, MCP, Async, Approval
- Modular document ingestion pipeline (PDF, DOCX, TXT, Markdown)
- Configurable chunking with metadata preservation
- Pluggable embedding providers (DeterministicFake, OpenAI)
- Vector store abstraction (InMemory, PgVector)
- RAG Agent with grounded context and citations
- LLM-assisted planning with validation and deterministic fallback
- MCP tool integration with adapter and permissions
- Async job system with queue, worker, retry, and idempotency
- Human approval workflow with risk classification
- Extended evaluation, observability, security controls
- 235 passing tests

### Phase 4 — Production Infrastructure, Real LLMs, Frontend

#### Real LLM Provider Integration
- OpenAI and Anthropic provider abstraction with retry, timeout, and structured output
- Deterministic provider preserved for tests
- Configurable model, temperature, max tokens, retry policy
- Token usage tracking and validation

#### Production PostgreSQL + pgvector
- Alembic migration for all Phase 3/4 tables
- pgvector extension for vector similarity search
- Document, chunk, job, approval persistence
- Tenant-scoped queries with organization isolation

#### Production Redis Async Execution
- Redis-backed job queue with connection pooling
- Worker process with graceful shutdown
- Job lifecycle: QUEUED → RUNNING → COMPLETED/FAILED/CANCELLED
- Idempotency, bounded retries, exponential backoff

#### LangGraph Checkpointing
- Workflow state survives process restarts
- Checkpoint after each node execution
- Resume from checkpoint on approval completion
- Approval pause/resume integration
- Workflow state sanitization (secrets removed)

#### Production Observability
- OpenTelemetry integration via otel-collector
- Prometheus metrics: request, LLM, workflow, tool, RAG, approval, queue metrics
- Grafana dashboards: system, AI, RAG dashboards
- Sensitive data filtering in traces

#### Advanced LLM Evaluation/Critic
- Deterministic evaluators for schema, state, tool success
- LLM-based critic for RAG relevance, planning quality, response correctness
- Combined evaluation with retry/fail decisions
- Configurable thresholds, max evaluation loops

#### React/Next.js Frontend
- Login/Register with JWT authentication
- Dashboard with stats, recent requests, pending approvals
- Create Request page with task submission
- Workflow Detail page with execution state and results
- Approval UI with approve/reject controls
- Documents & Knowledge Base with upload and management
- Execution Logs with sanitized audit events

#### API Endpoints Added
- `POST /api/v1/documents` — Upload and ingest documents
- `GET /api/v1/documents` — List documents (tenant-isolated)
- `GET /api/v1/documents/{id}` — Get document details
- `DELETE /api/v1/documents/{id}` — Delete document
- `GET /api/v1/approvals` — List pending approvals
- `POST /api/v1/approvals/{id}/approve` — Approve request
- `POST /api/v1/approvals/{id}/reject` — Reject request
- `GET /api/v1/evaluation/metrics` — Aggregate evaluation metrics
- `GET /api/v1/evaluation/metrics/{id}` — Per-request metrics
- `GET /metrics` — Prometheus metrics endpoint

#### Security Hardening
- Document upload validation (size, content type, safe filenames)
- Tenant isolation enforced across documents, chunks, jobs, approvals
- Prompt injection detection
- Secret detection in content
- Checkpoint state sanitization (API keys, tokens, passwords removed)
- Model output validation
- Cross-tenant access prevention

#### Docker Development Stack
```bash
docker compose up --build
```
Services: API, Worker, PostgreSQL, Redis, Frontend, OTel Collector, Prometheus, Grafana

#### CI/CD
- Python quality: ruff, mypy, pytest
- Frontend: npm lint, type-check
- Integration test workflow (optional, requires services)

### Phase 5 - Multi-Agent Orchestration and Operations

#### Multi-Agent Workflow Execution
- Dependency-aware task DAGs with validation for references, agent types, and cycles
- Execution waves with bounded parallelism for independent tasks
- Per-task retries, timeouts, failure policies, and approval gates
- Pause/resume support with checkpoint-friendly task records and durable workflow state
- Planner, Research, RAG, Analysis, Synthesis, and Evaluation agents
- Structured task outputs and evidence references passed to dependent agents

The planner uses the configured LLM provider when available and falls back to the
deterministic planner. Research and RAG tasks can feed evidence to Analysis;
Synthesis combines upstream results and citations, and workflow-level Evaluation
reports planning, collaboration, evidence transfer, conflicts, missing evidence,
redundancy, and final-response quality.

#### Advanced RAG
- Hybrid vector and lexical retrieval with reciprocal-rank fusion
- Deterministic or optional LLM reranking with fallback behavior
- Bounded query expansion
- Budgeted, deduplicated, source-diverse context assembly with citation mapping
- Deterministic retrieval-quality dataset and evaluation for exact, semantic, paraphrased, distractor, insufficient-context, and tenant-isolation cases
- Real PostgreSQL/pgvector hybrid retrieval integration tests covering vector, lexical, fusion, reranking, context/citation preservation, and tenant filtering

#### MCP Operations
- Registered MCP server and tool catalogs with public metadata only
- Connection lifecycle management and health monitoring
- Configured tool allow-lists and existing permission/risk/approval controls

#### Phase 5 APIs
- `GET /api/v1/workflows/{id}` - tenant-scoped workflow graph and task state
- `GET /api/v1/workflows/{id}/tasks` - tenant-scoped task execution state
- `GET /api/v1/workflows/{id}/evaluations` - workflow evaluation details
- `GET /api/v1/tools` - registered tools and MCP metadata without secrets
- `GET /api/v1/mcp/servers` - configured MCP server metadata
- `GET /api/v1/mcp/tools` - discovered MCP tool metadata

#### Phase 5 Validation Status
- **Implemented and unit-tested:** DAG scheduling, execution waves, bounded concurrency, retries, timeouts, failure policies, approval gates, evidence passing, hybrid RAG, MCP lifecycle, synthesis, and workflow evaluation.
- **Application-path tested:** authenticated API execution through LangGraph with Research, RAG, Analysis, Synthesis, persisted checkpoints, task introspection, and workflow evaluation.
- **Production boundary:** Redis is required for asynchronous production submission; Redis failures return `503` rather than using an in-memory queue. A live API-to-Redis-to-worker test passed with PostgreSQL/pgvector, Redis, and the separate worker running under Docker.
- **Known limitation:** Python thread-based task work cannot be force-cancelled. Timed-out work is marked terminal, late results are discarded, and the scheduler does not wait for the abandoned thread.

## Local Setup

### Prerequisites
- Python 3.12+
- Node.js 20+
- Git
- Docker Desktop (for PostgreSQL, Redis, observability stack)

### Quick Start

```bash
# Clone and setup
git clone <repository-url>
cd aegisforge
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

# Run tests
pytest -v

# Start infrastructure
docker compose up -d postgres redis

# Run the application
uvicorn aegisforge.app:app --reload

# Start worker (separate terminal)
python -m aegisforge.worker
```

### Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

Frontend runs at http://localhost:3000, proxying API requests to http://localhost:8000.

### Full Docker Stack

```bash
docker compose up --build
```

This starts: API, Worker, PostgreSQL, Redis, Frontend, OTel Collector, Prometheus, Grafana.

### Environment Variables

Copy `.env.example` to `.env` and configure:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///./aegisforge.db` | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `LLM_PROVIDER` | `openai` | LLM provider (openai, anthropic, deterministic) |
| `LLM_API_KEY` | — | LLM API key (from env only) |
| `LLM_MODEL` | `gpt-4o-mini` | Model to use |
| `EMBEDDING_PROVIDER` | `deterministic` | Embedding provider |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `EMBEDDING_DIMENSION` | `384` | Embedding vector dimension |

## Testing

```bash
# Backend tests (382 tests; integration tests are skipped unless enabled)
pytest -v

# With coverage
pytest --cov=aegisforge --cov-report=term-missing

# Real PostgreSQL + pgvector + Redis integration tests (requires services)
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
AEGISFORGE_INTEGRATION_TESTS=true pytest -q tests/integration

# Frontend tests
cd frontend && npm test

# Specific test file
pytest tests/test_workflow.py -v
```

> **Note:** The default `pytest` run reports `382 passed, 23 skipped`. The 23
> skipped are real-infrastructure integration tests that only run with
> `AEGISFORGE_INTEGRATION_TESTS=true` (23 passing when services are up). The
> frontend suite adds 35 component/behavioral tests via `npm test`. Real-LLM
> tests remain opt-in via `AEGISFORGE_REAL_LLM_TESTS=true` and are not run in CI.

### Test Categories

| Category | File | Tests | Description |
|----------|------|-------|-------------|
| Agent | `test_agents.py` | 14 | Agent abstraction, research, planner |
| Tool | `test_tools.py` | 20 | Tool abstraction, registry, permissions |
| Workflow | `test_workflow.py` | 17 | LangGraph, evaluation, retry logic |
| API | `test_api_auth_and_requests.py` | 1 | Auth and request flow |
| E2E | `test_e2e_acceptance.py` | 2 | Full pipeline, API contracts |
| Domain | `test_domain_models.py` | 2 | Domain model contracts |
| Legacy | `test_agent_workflow.py` | 3 | Updated legacy tests |
| RAG | `test_rag.py` | 30 | Ingestion, chunking, embeddings, vector store, retrieval, RAG agent |
| LLM Planner | `test_llm_planner.py` | 16 | LLM planner, validation, fallback |
| MCP | `test_mcp.py` | 17 | MCP client, adapter, permissions, manager |
| Async | `test_async_execution.py` | 15 | Job queue, manager, worker, retry |
| Approval | `test_approval.py` | 18 | Approval creation, decisions, expiry, safe actions |
| Security | `test_security.py` | 22 | Document validation, tenant isolation, injection detection |
| Observability | `test_observability.py` | 14 | Tracing, metrics collection |
| Evaluation | `test_evaluation.py` | 11 | RAG, plan, and execution evaluation |
| E2E Phase 3 | `test_e2e_phase3.py` | 4 | RAG, MCP, approval, async pipelines |
| Checkpoint | `test_checkpoint.py` | 18 | Checkpointing, approval pause/resume, sanitization |
| Security Phase 4 | `test_security_phase4.py` | 15 | Document security, prompt injection, tenant isolation, secrets |
| E2E Phase 4 | `test_e2e_phase4.py` | 11 | Full RAG pipeline, async workflow, approval lifecycle, failure recovery |
| Approval DB | `test_approval_db.py` | 18 | DB-backed approval persistence, decisions, expiry, tenant isolation |
| Checkpoint DB | `test_checkpoint_db.py` | 8 | Durable DB checkpoint store, restart recovery |
| Approval E2E | `test_approval_e2e.py` | 5 | API-level approval approve/reject/expire/unauthorized flow |
| Metrics Exec | `test_metrics_execution.py` | 8 | Prometheus counters/histograms change on real execution |
| Tracing | `test_tracing.py` | 6 | OTel tracing through execution path, failed spans |
| Rate Limit | `test_rate_limit.py` | 4 | Redis-backed rate limiting, configurable limits |
| Security 4.2 | `test_security_phase42.py` | 5 | Tenant isolation, authorization, secrets recheck |
| Integration | `tests/integration/` | 29 | Real PostgreSQL/pgvector + Redis (opt-in via env flag), including hybrid retrieval |
| Frontend | `frontend/src/**/*.test.*` | 35 | API client, auth, login, dashboard, approvals, documents, requests |

## Documentation

- [Architecture](docs/architecture/phase0-architecture.md)
- [Engineering Standards](docs/engineering-standards.md)
- [Testing Strategy](docs/testing/testing-strategy.md)
- [Use Cases](docs/requirements/use-cases.md)
- [ADR 0001: Modular Monolith](docs/adr/0001-modular-monolith.md)
- [ADR 0002: Agent/Tool/Workflow Execution](docs/adr/0002-agent-tool-workflow-execution.md)
- [ADR 0003: Combined Phase 3](docs/adr/0003-combined-phase3-rag-planner-mcp-async-approval.md)

## Known Limitations

- Deterministic/planner agents used by default; real LLM requires API keys
- pgvector requires PostgreSQL with pgvector extension (Docker Compose handles this)
- Redis async execution requires Redis running
- Frontend is a functional skeleton — needs production styling and error handling
- LLM critic requires a configured LLM provider
- No distributed workflow execution (single worker)
- No production monitoring/alerting rules

## License

Proprietary. Internal use only.
