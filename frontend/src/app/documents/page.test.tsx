import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DocumentsPage from "@/app/documents/page";

const { listDocumentsMock, uploadDocumentMock, deleteDocumentMock } = vi.hoisted(() => ({
  listDocumentsMock: vi.fn(),
  uploadDocumentMock: vi.fn(),
  deleteDocumentMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/components/Sidebar", () => ({
  default: () => <nav>Sidebar</nav>,
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    token: "tok",
    isAuthenticated: true,
    isLoading: false,
  }),
}));

vi.mock("@/lib/api", () => ({
  apiClient: {
    listDocuments: listDocumentsMock,
    uploadDocument: uploadDocumentMock,
    deleteDocument: deleteDocumentMock,
  },
}));

describe("DocumentsPage", () => {
  beforeEach(() => {
    listDocumentsMock.mockReset();
    uploadDocumentMock.mockReset();
    deleteDocumentMock.mockReset();
  });

  it("renders uploaded documents from the API", async () => {
    listDocumentsMock.mockResolvedValue({
      documents: [
        {
          id: "doc-1",
          title: "Architecture Overview",
          content_type: "text/markdown",
          chunk_count: 12,
          status: "completed",
          created_at: "now",
        },
      ],
      total: 1,
      page: 1,
      page_size: 50,
    });
    render(<DocumentsPage />);

    await waitFor(() => {
      expect(screen.getByText("Architecture Overview")).toBeInTheDocument();
    });
    expect(screen.getByText("12")).toBeInTheDocument();
  });

  it("uploads a file and refreshes the list", async () => {
    listDocumentsMock
      .mockResolvedValueOnce({ documents: [], total: 0, page: 1, page_size: 50 })
      .mockResolvedValueOnce({
        documents: [
          {
            id: "doc-2",
            title: "policy.md",
            content_type: "text/markdown",
            chunk_count: 5,
            status: "completed",
            created_at: "now",
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
      });
    uploadDocumentMock.mockResolvedValue({
      document_id: "doc-2",
      title: "policy.md",
      chunk_count: 5,
      status: "completed",
      message: "ok",
    });

    const user = userEvent.setup();
    render(<DocumentsPage />);

    const file = new File(["# Policy"], "policy.md", { type: "text/markdown" });
    const input = await screen.findByLabelText<HTMLInputElement>(/file/i);
    await user.upload(input, file);
    await user.click(screen.getByRole("button", { name: "Upload & Ingest" }));

    await waitFor(() => {
      expect(uploadDocumentMock).toHaveBeenCalledWith(file, "policy.md", "tok");
    });
    await waitFor(() => {
      expect(screen.getByText('Uploaded "policy.md" — 5 chunks created.')).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText("policy.md")).toBeInTheDocument();
    });
  });

  it("shows an upload error when ingestion fails", async () => {
    listDocumentsMock.mockResolvedValue({ documents: [], total: 0, page: 1, page_size: 50 });
    uploadDocumentMock.mockRejectedValue(new Error("Unsupported file type"));

    const user = userEvent.setup();
    render(<DocumentsPage />);

    const file = new File(["bad"], "broken.pdf", { type: "application/pdf" });
    const input = await screen.findByLabelText<HTMLInputElement>(/file/i);
    await user.upload(input, file);
    await user.click(screen.getByRole("button", { name: "Upload & Ingest" }));

    await waitFor(() => {
      expect(screen.getByText("Unsupported file type")).toBeInTheDocument();
    });
  });

  it("deletes a document after confirmation", async () => {
    listDocumentsMock.mockResolvedValue({
      documents: [
        {
          id: "doc-1",
          title: "To Delete",
          content_type: "text/plain",
          chunk_count: 1,
          status: "completed",
          created_at: "now",
        },
      ],
      total: 1,
      page: 1,
      page_size: 50,
    });
    deleteDocumentMock.mockResolvedValue(undefined);
    const confirmMock = vi.spyOn(window, "confirm").mockReturnValue(true);

    const user = userEvent.setup();
    render(<DocumentsPage />);

    await user.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(deleteDocumentMock).toHaveBeenCalledWith("doc-1", "tok");
    });
    await waitFor(() => {
      expect(screen.queryByText("To Delete")).not.toBeInTheDocument();
    });
    confirmMock.mockRestore();
  });

  it("does not delete when the user cancels the confirmation", async () => {
    listDocumentsMock.mockResolvedValue({
      documents: [
        {
          id: "doc-1",
          title: "Keep Me",
          content_type: "text/plain",
          chunk_count: 1,
          status: "completed",
          created_at: "now",
        },
      ],
      total: 1,
      page: 1,
      page_size: 50,
    });
    const confirmMock = vi.spyOn(window, "confirm").mockReturnValue(false);

    const user = userEvent.setup();
    render(<DocumentsPage />);

    await user.click(await screen.findByRole("button", { name: "Delete" }));

    expect(deleteDocumentMock).not.toHaveBeenCalled();
    expect(screen.getByText("Keep Me")).toBeInTheDocument();
    confirmMock.mockRestore();
  });
});