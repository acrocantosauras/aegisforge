import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ApiClient } from "@/lib/api";

describe("ApiClient", () => {
  let client: ApiClient;
  const fetchMock = vi.fn();

  beforeEach(() => {
    client = new ApiClient("http://test.local");
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends bearer token on authenticated requests", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve([{ id: "req-1" }]),
    });

    await client.listRequests("token-abc");

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers["Authorization"]).toBe("Bearer token-abc");
  });

  it("posts JSON bodies with content-type header", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ id: "req-1", status: "created" }),
    });

    await client.createRequest("Do a thing", { scope: "x" }, "tok");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://test.local/api/v1/requests");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({
      intent: "Do a thing",
      context: { scope: "x" },
    });
  });

  it("throws the backend detail message on error responses", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 401,
      json: () => Promise.resolve({ detail: "Invalid credentials" }),
    });

    await expect(client.login("a@b.com", "bad")).rejects.toThrow(
      "Invalid credentials"
    );
  });

  it("falls back to a status message when the error body is not JSON", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      json: () => Promise.reject(new Error("not json")),
    });

    await expect(client.healthCheck()).rejects.toThrow(/HTTP 500/);
  });

  it("sends multipart form data for document upload", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({ document_id: "doc-1", chunk_count: 3, status: "completed" }),
    });

    const file = new File(["content"], "report.txt", { type: "text/plain" });
    await client.uploadDocument(file, "Report", "tok");

    const [, init] = fetchMock.mock.calls[0];
    expect(init.body).toBeInstanceOf(FormData);
    expect(init.headers["Content-Type"]).toBeUndefined(); // fetch sets it
    expect(init.method).toBe("POST");
  });

  it("handles 204 no-content responses", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 204, json: () => Promise.reject() });

    await client.deleteDocument("doc-1", "tok");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});