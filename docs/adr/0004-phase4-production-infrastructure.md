# ADR 0004: Phase 4 — Production Infrastructure, Real LLMs, Checkpointed Workflows, Frontend

## Status

Accepted

## Context

Phase 3 established the architectural foundation for RAG, LLM planning, MCP integration, async execution, and human approval with 235 passing tests. However, the system lacked:

- Real LLM provider integration (only deterministic providers)
- Production database persistence (Alembic migrations incomplete)
- Real Redis async execution (in-memory queue only)
- Workflow checkpointing for durable execution
- Production observability (OpenTelemetry, Prometheus)
- Advanced LLM-based evaluation/critic
- Frontend application
- Complete API surface for all features

Phase 4 moves AegisForge from a strong architecture/test foundation toward a system that can run against real infrastructure and real users.

## Decision

### 1. LLM Provider Abstraction

**Decision**: Create a `ModelProvider` abstract class with `OpenAIModelProvider`, `AnthropicModelProvider`, and `DeterministicModelProvider` implementations.

**Rationale**: 
- Framework-independent abstraction prevents vendor lock-in
- Deterministic provider preserved for testing without API costs
- Configuration-driven provider selection via environment variables
- Retry, timeout, and structured output built into the abstraction

**Alternatives considered**:
- Using LangChain's LLM abstraction directly — rejected because it couples domain logic to a specific framework
- Hardcoding OpenAI — rejected because it prevents future provider switching

### 2. LangGraph Checkpointing

**Decision**: Implement an in-process `CheckpointStore` with `WorkflowCheckpointer` that saves state after each graph node execution.

**Rationale**:
- Enables workflow resume after process restart
- Enables approval pause/resume without losing context
- State sanitization removes secrets before persistence
- In-memory store for testing; PostgreSQL-backed store for production

**Alternatives considered**:
- LangGraph's built-in checkpointer — rejected because it would couple the workflow to LangGraph's persistence model
- External checkpoint service — premature for the current scale

### 3. Approval Pause/Resume

**Decision**: Approval-required tasks set `WAITING_FOR_APPROVAL` status, which pauses the LangGraph workflow. Approval decision triggers workflow resume from the latest checkpoint.

**Rationale**:
- Preserves workflow identity and execution context
- Does not start a new workflow on resume
- Checkpoint ensures the workflow can recover from any point
- Approval decision is recorded in the audit trail

### 4. Frontend Architecture

**Decision**: Next.js 14 with React 18, TypeScript strict mode, App Router.

**Rationale**:
- App Router provides server components for performance
- TypeScript strict mode catches type errors at build time
- API proxy rewrites through `next.config.js` simplify CORS
- JWT authentication via client-side `AuthProvider`

**Alternatives considered**:
- Vue/Nuxt — team familiarity with React
- SPA-only — rejected because server components improve initial load

### 5. Observability Stack

**Decision**: OpenTelemetry for distributed tracing, Prometheus for metrics, Grafana for dashboards.

**Rationale**:
- OpenTelemetry is the CNCF standard for observability
- Prometheus metrics expose real measured values (not fabricated)
- Grafana provides pre-configured dashboards for system, AI, and RAG
- OTel collector decouples instrumentation from backend

### 6. Docker Development Stack

**Decision**: Single `docker compose up --build` starts all services.

**Rationale**:
- Reduces onboarding friction for new developers
- All infrastructure dependencies (PostgreSQL, Redis, OTel, Prometheus, Grafana) included
- Health checks ensure service readiness
- `.env.example` documents required configuration

## Consequences

### Positive
- System can now run against real LLM providers, PostgreSQL, and Redis
- Workflow execution survives process restarts via checkpointing
- Frontend provides a complete user interface for all features
- Observability provides visibility into production behavior
- 279 tests pass, including 26 new tests for checkpointing, security, and E2E scenarios

### Negative
- Increased operational complexity (more services to deploy)
- LLM API costs when using real providers
- Frontend needs production styling and error handling work

### Risks
- LLM provider rate limits may affect throughput — mitigated by retry policies
- Checkpoint state size could grow for very long workflows — mitigated by state sanitization
- Single worker process limits throughput — identified as Phase 5 concern
