/**
 * API client for AegisForge backend.
 *
 * All communication goes through documented backend APIs only.
 * Authentication via JWT Bearer tokens.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const API_PREFIX = "/api/v1";

interface ApiOptions {
  method?: string;
  body?: unknown;
  token?: string;
  formData?: FormData;
}

interface ApiError {
  detail: string | { violations: string[] };
}

export class ApiClient {
  private baseUrl: string;

  constructor(baseUrl: string = API_BASE) {
    this.baseUrl = baseUrl;
  }

  private async request<T>(
    path: string,
    options: ApiOptions = {}
  ): Promise<T> {
    const { method = "GET", body, token, formData } = options;

    const headers: Record<string, string> = {};
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    let requestBody: string | FormData | undefined;
    if (formData) {
      requestBody = formData;
    } else if (body) {
      headers["Content-Type"] = "application/json";
      requestBody = JSON.stringify(body);
    }

    const response = await fetch(
      `${this.baseUrl}${API_PREFIX}${path}`,
      {
        method,
        headers,
        body: requestBody,
      }
    );

    if (!response.ok) {
      const errorData: ApiError = await response.json().catch(() => ({
        detail: `HTTP ${response.status}: ${response.statusText}`,
      }));
      throw new Error(
        typeof errorData.detail === "string"
          ? errorData.detail
          : JSON.stringify(errorData.detail)
      );
    }

    if (response.status === 204) {
      return undefined as T;
    }

    return response.json();
  }

  // Auth
  async register(email: string, password: string, fullName: string) {
    return this.request<{
      access_token: string;
      token_type: string;
    }>("/auth/register", {
      method: "POST",
      body: { email, password, full_name: fullName },
    });
  }

  async login(email: string, password: string) {
    return this.request<{
      access_token: string;
      token_type: string;
    }>("/auth/login", {
      method: "POST",
      body: { email, password },
    });
  }

  // Requests
  async createRequest(
    intent: string,
    context: Record<string, unknown> = {},
    token: string
  ) {
    return this.request<{
      id: string;
      organization_id: string;
      status: string;
    }>("/requests", { method: "POST", body: { intent, context }, token });
  }

  async listRequests(token: string) {
    return this.request<
      { id: string; intent: string; status: string; created_at: string }[]
    >("/requests", { token });
  }

  async getRequest(id: string, token: string) {
    return this.request<{
      id: string;
      intent: string;
      status: string;
      context: Record<string, unknown>;
      created_at: string;
    }>(`/requests/${id}`, { token });
  }

  // Execution
  async executeRequest(requestId: string, token: string) {
    return this.request<{
      request_id: string;
      workflow_id: string;
      status: string;
      final_result: Record<string, unknown>;
      errors: string[];
    }>(`/execution/requests/${requestId}/execute`, { method: "POST", token });
  }

  // Documents
  async uploadDocument(file: File, title: string, token: string) {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("title", title);

    return this.request<{
      document_id: string;
      title: string;
      chunk_count: number;
      status: string;
      message: string;
    }>("/documents", { method: "POST", formData, token });
  }

  async listDocuments(token: string) {
    return this.request<{
      documents: {
        id: string;
        title: string;
        content_type: string;
        chunk_count: number;
        status: string;
        created_at: string;
      }[];
      total: number;
      page: number;
      page_size: number;
    }>("/documents", { token });
  }

  async deleteDocument(id: string, token: string) {
    await this.request(`/documents/${id}`, { method: "DELETE", token });
  }

  // Approvals
  async listApprovals(token: string) {
    return this.request<{
      approvals: {
        approval_id: string;
        job_id: string;
        request_id: string;
        action_description: string;
        risk_level: string;
        status: string;
        reason: string;
        created_at: string;
      }[];
      total: number;
    }>("/approvals", { token });
  }

  async approveRequest(
    approvalId: string,
    decisionReason: string,
    token: string
  ) {
    return this.request<{
      approval_id: string;
      status: string;
    }>(`/approvals/${approvalId}/approve`, {
      method: "POST",
      body: { decision_reason: decisionReason },
      token,
    });
  }

  async rejectRequest(
    approvalId: string,
    decisionReason: string,
    token: string
  ) {
    return this.request<{
      approval_id: string;
      status: string;
    }>(`/approvals/${approvalId}/reject`, {
      method: "POST",
      body: { decision_reason: decisionReason },
      token,
    });
  }

  // Evaluation
  async getMetrics(token: string) {
    return this.request<{
      count: number;
      avg_workflow_duration_ms: number;
      total_tool_calls: number;
      total_retries: number;
      total_failures: number;
      success_rate: number;
    }>("/evaluation/metrics", { token });
  }

  // Audit
  async listAuditEvents(token: string, limit: number = 50) {
    return this.request<{
      events: {
        id: string;
        action: string;
        resource_type: string;
        resource_id: string;
        outcome: string;
        created_at: string;
      }[];
    }>(`/audit?limit=${limit}`, { token });
  }

  // Health
  async healthCheck() {
    return this.request<{ status: string }>("/health");
  }
}

export const apiClient = new ApiClient();
