# ADR 0003: Combined Phase 3 — RAG, LLM Planning, MCP, Async Execution, Human Approval

- Status: Accepted
- Date: 2026-09-03

## Context

Phase 2 completed with a working multi-agent execution pipeline: rule-based planner, Research Agent, deterministic knowledge tool, LangGraph workflow with retry logic, evaluation/critic, and full audit trail. All 61 tests pass.

The Phase 2 limitations are intentional and documented:
- Rule-based planner (no LLM reasoning)
- Single Research Agent type
- In-memory curated knowledge (no real retrieval)
- Synchronous execution only
- No MCP integration
- Human-approval state defined but not implemented

Phase 3 addresses these limitations as a coordinated set of changes. The components are deeply interrelated: RAG needs document ingestion and vector retrieval, LLM planning needs model abstraction and validation, async execution needs job infrastructure, and human approval needs workflow pause/resume.

## Decision: Production RAG / Knowledge System

### Document Ingestion Pipeline
- Modular pipeline: file handling → extraction → normalization → chunking → embedding → persistence
- Supported formats: PDF, DOCX, TXT, Markdown
- Each stage is independently testable
- No single monolithic ingestion function

### Chunking Strategy
- Configurable chunk size and overlap
- Metadata preserved per chunk: document ID, chunk ID, position, source, page/section, organization ID
- Chunks are independently testable

### Embedding Abstraction
- Abstract embedding provider interface (EmbeddingProvider)
- Provider is replaceable via configuration
- Support for a deterministic fake provider for testing
- Batching, dimensionality validation, retry on failure
- No hard-coded provider in domain layer

### Vector Storage
- Abstract vector store interface (VectorStore)
- PostgreSQL + pgvector implementation for production
- SQLite-compatible in-memory implementation for testing
- Stores: chunk content, embedding, metadata, ownership, document references
- Similarity search with configurable top-k and thresholds

### Retrieval Service
- Clean retrieval abstraction (RetrievalService)
- Semantic search, configurable top-k, similarity thresholds
- Metadata filtering, tenant filtering
- Returns structured RetrievalResult with source metadata
- Never exposes raw database queries to agents

### RAG Agent
- New agent type (RAGAgent) that extends BaseAgent
- Workflow: task → retrieve relevant chunks → build grounded context → LLM → structured response → citations
- Always retrieves only authorized content
- Distinguishes retrieved evidence from generated reasoning
- Handles insufficient context gracefully

## Decision: LLM-Assisted Planning

### Planner Architecture
- LLM planner produces structured ExecutionPlan output
- Plan validation runs deterministically before execution
- If LLM fails, times out, or produces invalid output, falls back to deterministic planner
- Deterministic planner is never removed

### Plan Validation
- Validates: schema, allowed agents, allowed tools, dependency references, duplicate task IDs, circular dependencies, malformed tasks, unauthorized capabilities, max task count, max workflow depth
- Invalid plans never reach execution

### Model Abstraction
- Abstract LLM provider interface (ModelProvider)
- Configuration: provider, model, temperature, timeout, token limits, retry behavior
- No hard-coded API keys
- Application/domain logic independent of model vendor

## Decision: MCP Tool Integration

### Architecture
- MCP client abstraction compatible with existing Tool interface
- MCP tools are wrapped via MCPToolAdapter to appear as AegisForge Tools
- Existing ToolRegistry remains the controlled entry point
- Agents never directly connect to MCP servers

### Security
- MCP servers are treated as external/untrusted
- Explicit server configuration (no user-supplied URLs)
- Permission checks on every MCP tool invocation
- Timeouts, validation, audit logging, error handling
- No arbitrary command execution through MCP

## Decision: Async Workflow Execution

### Job System
- Redis-backed job queue for background processing
- Job submission returns execution identifier
- Worker processes execute LangGraph workflows
- Status queries available via API

### Execution States
- Typed enum: QUEUED, RUNNING, WAITING_FOR_APPROVAL, RETRYING, COMPLETED, FAILED, CANCELLED
- Validated state transitions
- State persisted in database for recovery

### Failure Recovery
- Worker failure: job can be re-queued
- Redis interruption: state persisted in DB
- Model/tool timeout: configurable per-step timeouts
- Process restart: state recoverable from DB
- Idempotency mechanisms where appropriate
- No claims of exactly-once distributed execution

## Decision: Human Approval Workflow

### Approval Model
- Persistence: approval request, requested action, requester, reviewer, reason, risk level, status, timestamps, decision
- Authorization enforced server-side

### Workflow Integration
- LangGraph workflow supports reaching approval state and later resuming
- Workflow identity and execution context preserved
- Approval decisions recorded in audit trail

### Safe Actions
- No destructive real-world actions in this phase
- Simulated actions only: simulated service restart, simulated configuration change
- Architecture supports real actions behind explicit permissions later

## Alternatives Considered

1. Separate microservice for each new capability
   - Rejected: unjustified complexity for current scale, violates ADR 0001
   
2. Replace SQLite with PostgreSQL-only
   - Rejected: SQLite is sufficient for testing, PostgreSQL for production; abstraction layer handles both

3. Use LangChain agents for RAG
   - Rejected: would couple domain layer to LangChain; we use LangGraph for orchestration only

4. Implement full distributed execution with exactly-once guarantees
   - Rejected: not genuinely implementable without significant infrastructure; use idempotency instead

## Trade-offs

- Benefits: All five capabilities integrate with existing agent runtime, maintaining architectural consistency
- Costs: More complex codebase; careful testing needed for async state management and workflow pause/resume

## Consequences

- Phase 0-2 architecture preserved
- New capabilities extend existing abstractions rather than replacing them
- Deterministic planner remains as fallback
- All MCP access goes through controlled tool layer
- Human approval is enforced server-side
- Evaluation metrics are measurable, not fabricated
