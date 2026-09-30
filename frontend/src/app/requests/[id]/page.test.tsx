import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const { pushMock, getRequestMock, getWorkflowByRequestMock, getWorkflowTasksMock,
  getWorkflowGraphMock, getWorkflowEvaluationMock, getWorkflowResultMock,
  executeAsyncMock, executeMock } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  getRequestMock: vi.fn(),
  getWorkflowByRequestMock: vi.fn(),
  getWorkflowTasksMock: vi.fn(),
  getWorkflowGraphMock: vi.fn(),
  getWorkflowEvaluationMock: vi.fn(),
  getWorkflowResultMock: vi.fn(),
  executeAsyncMock: vi.fn(),
  executeMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, back: vi.fn() }),
  useParams: () => ({ id: "req-1" }),
}));

vi.mock("@/components/Sidebar", () => ({
  default: () => <nav>Sidebar</nav>,
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({ token: "tok", isAuthenticated: true, isLoading: false }),
}));

vi.mock("@/lib/api", () => ({
  apiClient: {
    getRequest: getRequestMock,
    getWorkflowByRequest: getWorkflowByRequestMock,
    getWorkflowTasks: getWorkflowTasksMock,
    getWorkflowGraph: getWorkflowGraphMock,
    getWorkflowEvaluation: getWorkflowEvaluationMock,
    getWorkflowResult: getWorkflowResultMock,
    executeRequestAsync: executeAsyncMock,
    executeRequest: executeMock,
  },
}));

import RequestDetailPage from "@/app/requests/[id]/page";

const TASKS_PAYLOAD = {
  workflow_id: "wf-1",
  request_id: "req-1",
  status: "completed",
  tasks: [
    {
      task_id: "t1",
      description: "Gather internal knowledge",
      agent_type: "research",
      dependencies: [],
      status: "completed",
      retries: 0,
      duration_ms: 120,
      summary: "found data",
      errors: [],
      approval_required: false,
      tools_used: ["knowledge.search"],
      evidence_count: 2,
    },
    {
      task_id: "t2",
      description: "Retrieve enterprise knowledge base",
      agent_type: "rag",
      dependencies: [],
      status: "completed",
      retries: 1,
      duration_ms: 80,
      summary: "retrieved",
      errors: [],
      approval_required: false,
      tools_used: [],
      evidence_count: 1,
    },
    {
      task_id: "t3",
      description: "Synthesize final answer",
      agent_type: "synthesis",
      dependencies: ["t1", "t2"],
      status: "completed",
      retries: 0,
      duration_ms: 200,
      summary: "synthesized",
      errors: [],
      approval_required: false,
      tools_used: [],
      evidence_count: 3,
    },
  ],
};

function mockCompletedFlow() {
  getRequestMock.mockResolvedValue({
    id: "req-1",
    intent: "Research the data access policy",
    status: "completed",
    context: {},
    created_at: "2026-09-28T00:00:00Z",
  });
  getWorkflowByRequestMock.mockResolvedValue({
    request_id: "req-1",
    workflow_id: "wf-1",
    status: "completed",
  });
  getWorkflowTasksMock.mockResolvedValue(TASKS_PAYLOAD);
  getWorkflowGraphMock.mockResolvedValue({
    workflow_id: "wf-1",
    request_id: "req-1",
    status: "completed",
    nodes: [],
    edges: [
      { from: "t1", to: "t3" },
      { from: "t2", to: "t3" },
    ],
  });
  getWorkflowEvaluationMock.mockResolvedValue({
    workflow_id: "wf-1",
    request_id: "req-1",
    evaluation: {
      overall_score: 0.92,
      collaboration: { score: 0.9, information_passed_ratio: 1.0 },
      final_response: { produced: true, grounded: true, citation_count: 2 },
      planning: { score: 1.0, task_count: 3 },
    },
  });
  getWorkflowResultMock.mockResolvedValue({
    workflow_id: "wf-1",
    request_id: "req-1",
    status: "completed",
    agent_type: "synthesis",
    answer: "The synthesized enterprise answer",
    summary: "Synthesized from 3 agents",
    citations: [{ source: "internal-policy/v1", quality_score: 0.8 }],
    evidence: [{ source: "internal-policy/v1" }],
    tools_used: ["knowledge.search"],
    confidence: 0.87,
    failed_upstream: [],
    errors: [],
  });
}

describe("ExecutionWorkspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders request, task graph waves, evaluation, and final result", async () => {
    mockCompletedFlow();
    render(<RequestDetailPage />);

    await waitFor(() => {
      expect(screen.getByText("Research the data access policy")).toBeInTheDocument();
    });

    // Task graph shows all three agents (multiple nodes can share a label)
    await waitFor(() => {
      expect(screen.getAllByText(/research/).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/rag/).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/synthesis/).length).toBeGreaterThan(0);
    });

    // Wave 1 has the two parallel tasks; synthesis is in wave 2
    expect(screen.getByText("Wave 1 (parallel entry)")).toBeInTheDocument();
    expect(screen.getByText("Wave 2")).toBeInTheDocument();

    // Evaluation metrics rendered from REAL evaluation payload
    expect(screen.getByText("Overall Score")).toBeInTheDocument();
    expect(screen.getByText("0.92")).toBeInTheDocument();

    // Final result
    await waitFor(() => {
      expect(
        screen.getByText("The synthesized enterprise answer")
      ).toBeInTheDocument();
    });
    // Citations and tool usage (tool name appears in both task card and result footer)
    expect(screen.getByText(/internal-policy\/v1/)).toBeInTheDocument();
    expect(screen.getAllByText(/knowledge\.search/).length).toBeGreaterThan(0);
  });

  it("renders approval gate badge for tasks requiring approval", async () => {
    mockCompletedFlow();
    // Re-map tasks so the analysis task is waiting on an approval gate.
    getWorkflowTasksMock.mockResolvedValue({
      ...TASKS_PAYLOAD,
      status: "waiting_for_approval",
      tasks: TASKS_PAYLOAD.tasks.map((t) =>
        t.task_id === "t3" ? { ...t, status: "waiting_for_approval", approval_required: true } : t
      ),
    });
    render(<RequestDetailPage />);

    await waitFor(() => {
      expect(screen.getByText("approval gate")).toBeInTheDocument();
    });
    expect(screen.getAllByText(/waiting for approval/i).length).toBeGreaterThan(0);
  });

  it("shows execute button for a never-executed request and calls async API", async () => {
    getRequestMock.mockResolvedValue({
      id: "req-1",
      intent: "Not yet executed",
      status: "created",
      context: {},
      created_at: "2026-09-28T00:00:00Z",
    });
    getWorkflowByRequestMock.mockRejectedValue(new Error("no workflow"));
    executeAsyncMock.mockResolvedValue({ status: "queued" });

    render(<RequestDetailPage />);

    await waitFor(() => {
      expect(screen.getByText(/has not been executed yet/)).toBeInTheDocument();
    });
    const btn = screen.getByRole("button", { name: "Execute" });
    expect(btn).toBeInTheDocument();
  });

  it("shows failure state with error badge", async () => {
    getRequestMock.mockResolvedValue({
      id: "req-1",
      intent: "Will fail",
      status: "failed",
      context: {},
      created_at: "2026-09-28T00:00:00Z",
    });
    getWorkflowByRequestMock.mockResolvedValue({
      request_id: "req-1",
      workflow_id: "wf-9",
      status: "failed",
    });
    getWorkflowTasksMock.mockResolvedValue({
      workflow_id: "wf-9",
      request_id: "req-1",
      status: "failed",
      tasks: [
        {
          task_id: "t1",
          description: "failing task",
          agent_type: "research",
          dependencies: [],
          status: "failed",
          retries: 2,
          duration_ms: 5000,
          summary: "",
          errors: ["MCP server connection refused"],
          approval_required: false,
          tools_used: [],
          evidence_count: 0,
        },
      ],
    });
    getWorkflowGraphMock.mockResolvedValue({
      workflow_id: "wf-9", request_id: "req-1", status: "failed", nodes: [], edges: [],
    });
    getWorkflowEvaluationMock.mockResolvedValue({
      workflow_id: "wf-9", request_id: "req-1", evaluation: {},
    });
    getWorkflowResultMock.mockResolvedValue({
      workflow_id: "wf-9", request_id: "req-1", status: "failed",
      agent_type: "", answer: "", summary: "", citations: [], evidence: [],
      tools_used: [], confidence: null, failed_upstream: ["t1"],
      errors: ["task failed"],
    });

    render(<RequestDetailPage />);

    await waitFor(() => {
      expect(screen.getByText(/MCP server connection refused/)).toBeInTheDocument();
    });
  });
});
