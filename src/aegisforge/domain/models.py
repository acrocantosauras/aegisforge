from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Role(str, Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    USER = "user"
    AUDITOR = "auditor"


class RequestStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    REQUIRES_REVIEW = "requires_review"
    RETRYING = "retrying"
    ACTION_REQUIRES_APPROVAL = "action_requires_approval"


class AgentExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DENIED = "denied"


class ToolExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DENIED = "denied"


class EvaluationVerdict(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    RETRY = "retry"
    REQUIRES_REVIEW = "requires_review"


class AgentType(str, Enum):
    PLANNER = "planner"
    RESEARCH = "research"
    RAG = "rag"
    CODE = "code"
    VISION = "vision"
    EVALUATOR = "evaluator"


class ExecutionJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PlannerType(str, Enum):
    LLM = "llm"
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"


class EntityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Organization(EntityModel):
    name: str


class User(EntityModel):
    organization_id: str
    email: EmailStr
    role: Role
    is_active: bool = True


class Session(EntityModel):
    organization_id: str
    user_id: str
    title: str


class ToolPermission(BaseModel):
    name: str
    allowed: bool


class Request(EntityModel):
    organization_id: str
    requested_by: str
    intent: str
    status: RequestStatus = RequestStatus.PENDING
    permissions: list[ToolPermission] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class Task(EntityModel):
    request_id: str
    title: str
    description: str
    agent_type: AgentType
    status: RequestStatus = RequestStatus.PENDING
    dependencies: list[str] = Field(default_factory=list)


class Workflow(EntityModel):
    organization_id: str
    name: str
    definition: dict[str, Any] = Field(default_factory=dict)
    version: str = "1.0"


class Agent(EntityModel):
    organization_id: str
    name: str
    agent_type: AgentType
    description: str
    version: str = "1.0"
    capabilities: list[str] = Field(default_factory=list)


class Tool(EntityModel):
    organization_id: str
    name: str
    description: str
    version: str = "1.0"
    permissions: list[str] = Field(default_factory=list)


class KnowledgeSource(EntityModel):
    organization_id: str
    name: str
    source_type: str
    uri: str
    is_active: bool = True


class Document(EntityModel):
    knowledge_source_id: str
    title: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    document_id: str
    score: float
    snippet: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evaluation(EntityModel):
    request_id: str
    quality_score: float | None = None
    passed: bool | None = None
    notes: str = ""


class WorkflowEvent(EntityModel):
    workflow_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(EntityModel):
    organization_id: str
    actor_id: str | None = None
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlanTask(BaseModel):
    task_id: str
    description: str
    assigned_agent_type: AgentType
    input_data: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    expected_output_description: str = ""
    tool_permissions_required: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    plan_id: str
    request_id: str
    tasks: list[ExecutionPlanTask] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    agent_name: str
    agent_type: AgentType
    status: AgentExecutionStatus
    summary: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float | None = None
    errors: list[str] = Field(default_factory=list)
    execution_time_ms: int | None = None


class EvaluationResult(BaseModel):
    verdict: EvaluationVerdict
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolCallRecord(BaseModel):
    tool_name: str
    input_data: dict[str, Any] = Field(default_factory=dict)
    output_data: dict[str, Any] = Field(default_factory=dict)
    status: ToolExecutionStatus = ToolExecutionStatus.PENDING
    error: str | None = None
    duration_ms: int | None = None


class WorkflowExecutionState(BaseModel):
    request_id: str
    workflow_id: str
    plan: ExecutionPlan | None = None
    agent_results: list[AgentResult] = Field(default_factory=list)
    evaluation: EvaluationResult | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 3
    current_step: str = ""
    status: RequestStatus = RequestStatus.CREATED
    errors: list[str] = Field(default_factory=list)
    audit_events: list[dict[str, Any]] = Field(default_factory=list)


class DocumentChunk(BaseModel):
    chunk_id: str
    document_id: str
    content: str
    position: int = 0
    source: str = ""
    page: int | None = None
    section: str | None = None
    organization_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddingVector(BaseModel):
    chunk_id: str
    embedding: list[float]
    dimension: int = 0


class DocumentIngestionRequest(BaseModel):
    document_id: str
    organization_id: str
    title: str
    content: str
    content_type: str = "text/plain"
    source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalQuery(BaseModel):
    query: str
    organization_id: str
    top_k: int = 5
    similarity_threshold: float = 0.5
    metadata_filter: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    chunk_id: str
    document_id: str
    content: str
    score: float
    source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MCPServerConfig(BaseModel):
    server_id: str
    name: str
    transport: str = "stdio"  # stdio, sse
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    timeout_seconds: int = 30
    enabled: bool = True


class MCPToolDefinition(BaseModel):
    name: str
    description: str = ""
    server_id: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class ExecutionJob(BaseModel):
    job_id: str
    request_id: str
    workflow_id: str
    status: ExecutionJobStatus = ExecutionJobStatus.QUEUED
    retry_count: int = 0
    max_retries: int = 3
    result: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    idempotency_key: str = ""


class ApprovalRequest(BaseModel):
    approval_id: str
    job_id: str
    request_id: str
    workflow_id: str
    organization_id: str = ""
    action_description: str
    requested_by: str
    reviewer_id: str | None = None
    risk_level: RiskLevel = RiskLevel.MEDIUM
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str = ""
    decision_reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    expires_at: datetime | None = None


class LLMPlanResult(BaseModel):
    plan: ExecutionPlan
    planner_type: PlannerType
    model_used: str = ""
    tokens_used: int = 0
    latency_ms: int = 0
    validation_passed: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str = ""
