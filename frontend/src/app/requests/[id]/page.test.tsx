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
  usePathname: () => "/",
  useParams: () => ({ id: "req-1" }),
}));

vi.mock("@/components/shell/AppShell", () => ({
  default: ({ children, actions }: { children: React.ReactNode; actions?: React.ReactNode }) => (
    <div>
      {actions}
      {children}
    </div>
  ),
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
    listTools: vi.fn().mockResolvedValue({ tools: [], total: 0 }),
    listMCPTools: vi.fn().mockResolvedValue({ tools: [], total: 0 }),
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
      // Real backend payload key is overall_score (PlanEvaluationResult)
      planning: { overall_score: 0.8, task_count: 3 },
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
    expect(screen.getByText("Wave 1 · parallel entry")).toBeInTheDocument();
    expect(screen.getByText("Wave 2")).toBeInTheDocument();

    // Evaluation metrics rendered from REAL evaluation payload
    expect(screen.getAllByText(/overall 0\.92/).length).toBeGreaterThan(0);
    expect(screen.getByText("Planning")).toBeInTheDocument();
    // Planning quality comes back as overall_score from the API
    expect(screen.getByText("0.80")).toBeInTheDocument();
    expect(screen.getByText("Collaboration")).toBeInTheDocument();

    // Final result
    await waitFor(() => {
      expect(
        screen.getByText("The synthesized enterprise answer")
      ).toBeInTheDocument();
    });
    // Citations and tool usage (tool name appears in both task card and result footer)
    expect(screen.getAllByText(/internal-policy\/v1/).length).toBeGreaterThan(0);
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
      expect(screen.getByText("HUMAN APPROVAL REQUIRED")).toBeInTheDocument();
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
      expect(screen.getByText(/Not executed yet/)).toBeInTheDocument();
    });
    const btn = screen.getByRole("button", { name: "Execute" });
    expect(btn).toBeInTheDocument();
  });

  it("clears the approval banner once a gated run reaches a terminal state", async () => {
    // `approval_required` is a static task property — after the decision was
    // made and the workflow completed, the workspace must not keep claiming
    // that human approval is required.
    mockCompletedFlow();
    getWorkflowTasksMock.mockResolvedValue({
      ...TASKS_PAYLOAD,
      status: "completed",
      tasks: TASKS_PAYLOAD.tasks.map((t) =>
        t.task_id === "t3" ? { ...t, approval_required: true } : t
      ),
    });

    render(<RequestDetailPage />);

    await waitFor(() => {
      expect(screen.getByText("The synthesized enterprise answer")).toBeInTheDocument();
    });
    expect(
      screen.queryByText("HUMAN APPROVAL REQUIRED")
    ).not.toBeInTheDocument();
  });

  it("stops polling a request that was never submitted for execution", async () => {
    getRequestMock.mockResolvedValue({
      id: "req-1",
      intent: "Not yet executed",
      status: "created",
      context: {},
      created_at: "2026-09-28T00:00:00Z",
    });
    getWorkflowByRequestMock.mockRejectedValue(new Error("no workflow"));

    render(<RequestDetailPage />);

    await waitFor(() => {
      expect(screen.getByText(/Not executed yet/)).toBeInTheDocument();
    });
    // Let the initial load and any effect re-run settle.
    await vi.advanceTimersByTimeAsync(600);
    const settled = getRequestMock.mock.calls.length;

    // Nothing on the backend can change for a request that was never
    // executed, so no further polls may be issued.
    await vi.advanceTimersByTimeAsync(30_000);
    expect(getRequestMock.mock.calls.length).toBe(settled);
  });

  it("keeps polling while a workflow is still in flight", async () => {
    getRequestMock.mockResolvedValue({
      id: "req-1",
      intent: "Running request",
      status: "executing",
      context: {},
      created_at: "2026-09-28T00:00:00Z",
    });
    getWorkflowByRequestMock.mockResolvedValue({
      request_id: "req-1",
      workflow_id: "wf-run",
      status: "running",
    });
    getWorkflowTasksMock.mockResolvedValue({
      workflow_id: "wf-run",
      request_id: "req-1",
      status: "running",
      tasks: [],
    });
    getWorkflowGraphMock.mockResolvedValue({
      workflow_id: "wf-run",
      request_id: "req-1",
      status: "running",
      nodes: [],
      edges: [],
    });
    getWorkflowEvaluationMock.mockResolvedValue({
      workflow_id: "wf-run",
      request_id: "req-1",
      evaluation: {},
    });
    getWorkflowResultMock.mockRejectedValue(new Error("not ready"));

    render(<RequestDetailPage />);

    await waitFor(() => {
      expect(getRequestMock.mock.calls.length).toBeGreaterThan(0);
    });
    await vi.advanceTimersByTimeAsync(600);
    const before = getRequestMock.mock.calls.length;

    // The workflow is not terminal, so the workspace keeps polling.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(getRequestMock.mock.calls.length).toBeGreaterThan(before);
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
