# ADR 0002: Agent Runtime, Tool System, and LangGraph Execution Foundation

- Status: Accepted
- Date: 2026-09-02

## Context

Phase 1 established the platform foundation: FastAPI backend, PostgreSQL persistence, authentication, request/task management, and testing infrastructure. Phase 2 must transform AegisForge from a static platform into a genuine controlled multi-agent execution system capable of decomposing requests, executing specialized agents through approved tools, evaluating results, and retrying failures.

The key challenge is providing real agent execution while maintaining strict security boundaries, deterministic behavior for critical paths, and testability without requiring live LLM APIs.

## Decision

We implement a layered execution architecture with the following key decisions:

### 1. Agent Abstraction

**Decision**: Abstract base class (`BaseAgent`) with typed input/output (`AgentResult`), lifecycle tracking, and permission gating.

- Agents receive `AgentExecutionContext` containing only traceability identifiers (no secrets, no credentials)
- `BaseAgent.execute()` wraps `_execute()` with timing, exception handling, and status conversion
- `PermissionError` → DENIED status; all other exceptions → FAILED status
- Agents declare `PermissionSpec` lists; `check_permissions()` enforces before execution

**Alternatives considered**:
- Function-based agents (no class hierarchy): rejected because lifecycle tracking and permission enforcement belong in a shared base
- LangChain AgentExecutor: rejected because it couples the domain to LangChain's API surface

### 2. Tool Abstraction

**Decision**: `BaseTool` with `ToolDefinition` metadata, `ToolExecutionResult` outcomes, and permission-gated execution.

- Tools define: name, version, input/output schemas, permission requirements, timeout, approval requirements
- `BaseTool.execute()` handles: permission check → timing → execution → exception wrapping
- Returns `ToolExecutionResult` with status, output, error, duration, and execution ID
- Tools are independently testable without any framework coupling

**Alternatives considered**:
- LangChain tools: rejected because they don't support our permission model or timeout handling
- Raw function tools: rejected because we need declarative metadata for the registry

### 3. Tool Registry

**Decision**: Central `ToolRegistry` singleton that controls tool discovery, permission validation, and execution routing.

- Tools registered at startup (no runtime registration from untrusted users)
- Agents discover and execute tools through the registry, never directly
- Permission chain: User permissions → Agent permissions → Tool permissions
- Designed for future MCP tool integration

### 4. LangGraph Workflow

**Decision**: `StateGraph(dict)` with deterministic node functions and conditional edges for retry logic.

- Flow: validate → plan → execute_agent → evaluate → retry_or_complete → (execute_agent | END)
- Each node returns full state copy (required for `StateGraph(dict)` in current LangGraph version)
- No LLM calls in the workflow itself; all planning uses deterministic rule-based decomposition
- Retry mechanism: configurable max retries, retry count tracking, terminal failure state

**Alternatives considered**:
- Async workflow with Redis queue: deferred until actual concurrency requirements exist
- LangGraph with Pydantic state: deferred because `dict` state works and is simpler for initial phase

### 5. Planning

**Decision**: Rule-based deterministic planner that decomposes intents into `ExecutionPlan` with `ExecutionPlanTask` entries.

- Planner produces structured `ExecutionPlan` (validated before execution)
- Task assignment based on keyword matching against agent capabilities
- No LLM dependency for planning (deterministic, testable, auditable)
- Future: LLM-assisted planning with output validation

### 6. Evaluation

**Decision**: `ResultEvaluator` that scores agent results against structural requirements.

- Checks: status completion, required fields, summary presence, absence of errors, tool call success
- Returns `EvaluationResult` with verdict (PASSED/FAILED/RETRY), score, reasons, and retryability
- Score threshold: >= 0.7 for PASSED; lower scores trigger RETRY or FAILED
- No semantic quality claims (evaluated only structural correctness)

### 7. Retry/Recovery

**Decision**: Configurable retry with max count, retry classification, and terminal failure state.

- Retry conditions: timeout, transient tool failure, partial results
- Non-retryable: permission denied, invalid input, authorization failures
- Max retries configurable per workflow (default: 3)
- Terminal failure records error and stops execution

### 8. Human Approval Foundation

**Decision**: Define `ACTION_REQUIRES_APPROVAL` state in `RequestStatus` without implementing dangerous actions.

- Architecture supports future: agent recommendation → risk assessment → human approval → action execution
- No autonomous dangerous actions implemented
- State and interface established for future human-in-the-loop workflows

### 9. LangChain Integration

**Decision**: Minimal integration — `langchain-core` and `langgraph` for workflow orchestration only.

- LangGraph provides the graph execution engine (nodes, edges, conditional routing)
- No LangChain agents, chains, or prompt templates used
- Domain layer remains framework-independent
- Model provider can be changed without rewriting application logic

## Trade-offs

- Benefits: Strong security boundaries, deterministic behavior, full testability without LLM APIs, clear audit trail
- Costs: Rule-based planning is limited compared to LLM-assisted planning (deferred to future phase)

## Consequences

- The execution pipeline is fully testable with deterministic inputs
- Permission enforcement is enforced at every layer (user → agent → tool)
- All execution steps are auditable and traceable
- Future agents can be added by implementing `BaseAgent._execute()` and registering tools
- Future LLM integration can be added to planning/evaluation without changing the workflow structure
