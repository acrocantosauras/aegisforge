"""Tests for the RAG/Knowledge system.

Covers: document ingestion, text extraction, normalization, chunking,
embedding abstraction, vector storage, retrieval, authorization filtering,
tenant isolation, and RAG agent.
"""
from __future__ import annotations

import pytest

from aegisforge.agents.base import AgentExecutionContext, PermissionSpec
from aegisforge.agents.rag_agent import RAGAgent
from aegisforge.domain.models import AgentExecutionStatus, RetrievalQuery
from aegisforge.rag.embeddings import DeterministicEmbeddingProvider, get_embedding_provider
from aegisforge.rag.ingestion import (
    chunk_text,
    extract_text,
    extract_text_from_markdown,
    extract_text_from_plain,
    ingest_document,
    normalize_text,
)
from aegisforge.rag.retrieval import RetrievalService
from aegisforge.rag.vector_store import (
    InMemoryVectorStore,
    VectorStoreEntry,
    _cosine_similarity,
)


# --- Embedding Tests ---


def test_deterministic_embedding_produces_vectors() -> None:
    provider = DeterministicEmbeddingProvider(dimension=128)
    vectors = provider.embed_texts(["hello world", "test text"])
    assert len(vectors) == 2
    assert len(vectors[0]) == 128
    assert len(vectors[1]) == 128


def test_deterministic_embedding_is_deterministic() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    v1 = provider.embed_text("same input")
    v2 = provider.embed_text("same input")
    assert v1 == v2


def test_deterministic_embedding_different_inputs_different_vectors() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    v1 = provider.embed_text("alpha")
    v2 = provider.embed_text("beta")
    assert v1 != v2


def test_deterministic_embedding_normalized() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    v = provider.embed_text("test")
    norm = sum(x * x for x in v) ** 0.5
    assert abs(norm - 1.0) < 0.01


def test_embedding_provider_factory() -> None:
    provider = get_embedding_provider("deterministic", dimension=32)
    assert isinstance(provider, DeterministicEmbeddingProvider)
    assert provider.dimension == 32


def test_embedding_provider_factory_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        get_embedding_provider("nonexistent")


# --- Text Extraction Tests ---


def test_extract_text_plain() -> None:
    result = extract_text_from_plain(b"Hello world")
    assert result == "Hello world"


def test_extract_text_markdown() -> None:
    result = extract_text_from_markdown(b"# Title\n\nContent here")
    assert "# Title" in result
    assert "Content here" in result


def test_extract_text_unsupported_type() -> None:
    with pytest.raises(ValueError, match="Unsupported content type"):
        extract_text(b"content", "application/unknown")


def test_extract_text_dispatches_by_type() -> None:
    result = extract_text(b"test content", "text/plain")
    assert result == "test content"


# --- Normalization Tests ---


def test_normalize_text_removes_extra_whitespace() -> None:
    result = normalize_text("Hello   world\n\n\n\n\nNew paragraph")
    assert "   " not in result
    assert "\n\n\n" not in result


def test_normalize_text_removes_control_chars() -> None:
    result = normalize_text("Hello\x00world\x07test")
    assert "\x00" not in result
    assert "\x07" not in result


def test_normalize_empty_text() -> None:
    assert normalize_text("") == ""
    assert normalize_text("   ") == ""


# --- Chunking Tests ---


def test_chunk_text_basic() -> None:
    text = "A" * 1000
    chunks = chunk_text(text, chunk_size=200, chunk_overlap=50)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.chunk_id
        assert chunk.content
        assert chunk.position >= 0


def test_chunk_text_preserves_metadata() -> None:
    text = "Some content here"
    chunks = chunk_text(
        text,
        document_id="doc-1",
        source="test-source",
        metadata={"org": "test-org"},
    )
    assert len(chunks) >= 1
    assert chunks[0].document_id == "doc-1"
    assert chunks[0].source == "test-source"
    assert chunks[0].metadata.get("org") == "test-org"


def test_chunk_text_empty() -> None:
    chunks = chunk_text("")
    assert chunks == []


def test_chunk_text_small_content() -> None:
    chunks = chunk_text("Short text", chunk_size=512)
    assert len(chunks) == 1
    assert chunks[0].content == "Short text"


def test_chunk_text_preserves_position() -> None:
    text = "A" * 2000
    chunks = chunk_text(text, chunk_size=200, chunk_overlap=50)
    positions = [c.position for c in chunks]
    assert positions == list(range(len(chunks)))


# --- Ingestion Pipeline Tests ---


def test_ingest_document_plaintext() -> None:
    content = b"This is a test document with multiple paragraphs.\n\nSecond paragraph here."
    result = ingest_document(
        content=content,
        title="Test Doc",
        content_type="text/plain",
        organization_id="org-1",
    )
    assert result.document_id
    assert result.title == "Test Doc"
    assert len(result.chunks) >= 1
    assert result.content_hash


def test_ingest_document_markdown() -> None:
    content = b"# Title\n\nContent paragraph.\n\n## Section\n\nMore content."
    result = ingest_document(
        content=content,
        title="MD Doc",
        content_type="text/markdown",
    )
    assert len(result.chunks) >= 1


def test_ingest_document_empty() -> None:
    result = ingest_document(content=b"", title="Empty")
    assert result.chunks == []


def test_ingest_document_large_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds maximum size"):
        ingest_document(content=b"x" * (10_485_761), title="Huge")


# --- Vector Store Tests ---


def test_in_memory_vector_store_add_and_search() -> None:
    store = InMemoryVectorStore()
    provider = DeterministicEmbeddingProvider(dimension=64)

    texts = ["support escalation policy", "incident response procedure", "data access rules"]
    vectors = provider.embed_texts(texts)

    entries = [
        VectorStoreEntry(
            id=f"chunk-{i}",
            content=text,
            embedding=vec,
            metadata={"document_id": f"doc-{i}", "source": f"source-{i}"},
        )
        for i, (text, vec) in enumerate(zip(texts, vectors))
    ]

    store.add(entries, organization_id="org-1")

    # Search
    query_vec = provider.embed_text("support escalation")
    results = store.search(query_vec, top_k=2, organization_id="org-1")
    assert len(results) > 0
    assert results[0].score > 0


def test_in_memory_vector_store_tenant_isolation() -> None:
    store = InMemoryVectorStore()
    provider = DeterministicEmbeddingProvider(dimension=64)

    vec = provider.embed_text("secret document")
    store.add(
        [VectorStoreEntry(id="c1", content="Secret doc", embedding=vec)],
        organization_id="org-1",
    )
    store.add(
        [VectorStoreEntry(id="c2", content="Other doc", embedding=provider.embed_text("other"))],
        organization_id="org-2",
    )

    # Search as org-1 should only find org-1's data
    results = store.search(vec, top_k=10, organization_id="org-1")
    # Results should only contain entries from org-1
    result_ids = {r.id for r in results}
    assert "c1" in result_ids
    assert "c2" not in result_ids


def test_in_memory_vector_store_delete() -> None:
    store = InMemoryVectorStore()
    provider = DeterministicEmbeddingProvider(dimension=64)

    vec = provider.embed_text("test")
    store.add(
        [
            VectorStoreEntry(id="c1", content="A", embedding=vec, metadata={"document_id": "d1"}),
            VectorStoreEntry(id="c2", content="B", embedding=vec, metadata={"document_id": "d2"}),
        ]
    )

    deleted = store.delete_by_document("d1")
    assert deleted == 1
    assert store.count() == 1


def test_in_memory_vector_store_count() -> None:
    store = InMemoryVectorStore()
    provider = DeterministicEmbeddingProvider(dimension=64)
    vec = provider.embed_text("test")

    store.add(
        [
            VectorStoreEntry(id="c1", content="A", embedding=vec),
            VectorStoreEntry(id="c2", content="B", embedding=vec),
        ],
        organization_id="org-1",
    )
    assert store.count() == 2
    assert store.count("org-1") == 2
    assert store.count("org-2") == 0


def test_cosine_similarity_identical() -> None:
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_cosine_similarity_orthogonal() -> None:
    assert abs(_cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 0.01


def test_cosine_similarity_different_lengths() -> None:
    assert _cosine_similarity([1.0], [1.0, 0.0]) == 0.0


# --- Retrieval Service Tests ---


def test_retrieval_service_basic() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    store = InMemoryVectorStore()

    # Add some data
    texts = ["support escalation policy", "incident response"]
    vectors = provider.embed_texts(texts)
    entries = [
        VectorStoreEntry(id=f"c{i}", content=t, embedding=v, metadata={"source": f"s{i}"})
        for i, (t, v) in enumerate(zip(texts, vectors))
    ]
    store.add(entries, organization_id="org-1")

    service = RetrievalService(embedding_provider=provider, vector_store=store)
    query = RetrievalQuery(query="support escalation", organization_id="org-1", top_k=2, similarity_threshold=0.0)
    results = service.retrieve(query)

    assert len(results) > 0
    assert results[0].content
    assert results[0].score > 0


def test_retrieval_service_tenant_isolation() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    store = InMemoryVectorStore()

    vec = provider.embed_text("secret")
    store.add(
        [VectorStoreEntry(id="c1", content="Secret", embedding=vec)],
        organization_id="org-1",
    )
    store.add(
        [VectorStoreEntry(id="c2", content="Other", embedding=provider.embed_text("other"))],
        organization_id="org-2",
    )

    service = RetrievalService(embedding_provider=provider, vector_store=store)
    query = RetrievalQuery(query="secret", organization_id="org-1", top_k=10)
    results = service.retrieve(query)

    # Should only find org-1's data
    assert all("Secret" in r.content or "secret" in r.content.lower() for r in results)


def test_retrieval_service_empty_query() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    store = InMemoryVectorStore()
    service = RetrievalService(embedding_provider=provider, vector_store=store)

    results = service.retrieve(RetrievalQuery(query="", organization_id="org-1"))
    assert results == []


def test_retrieval_service_build_context() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    store = InMemoryVectorStore()
    service = RetrievalService(embedding_provider=provider, vector_store=store)

    # Add data
    vec = provider.embed_text("policy")
    store.add(
        [VectorStoreEntry(id="c1", content="Support policy text", embedding=vec, metadata={"source": "doc1"})],
        organization_id="org-1",
    )

    query = RetrievalQuery(query="policy", organization_id="org-1", top_k=1)
    results = service.retrieve(query)
    context = service.build_context(results)
    assert "Retrieved evidence" in context
    assert "Support policy text" in context


def test_retrieval_service_build_context_empty() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    store = InMemoryVectorStore()
    service = RetrievalService(embedding_provider=provider, vector_store=store)

    context = service.build_context([])
    assert context == ""


# --- RAG Agent Tests ---


def _make_rag_context(**overrides) -> AgentExecutionContext:
    defaults = {
        "request_id": "req-rag-test",
        "user_id": "user-rag-test",
        "organization_id": "org-rag-test",
        "permissions": [PermissionSpec(name="knowledge.search", allow=True)],
    }
    defaults.update(overrides)
    return AgentExecutionContext(**defaults)


def test_rag_agent_with_retrieval() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    store = InMemoryVectorStore()

    # Seed with data
    texts = ["support escalation requires manager approval", "incident response within 15 minutes"]
    vectors = provider.embed_texts(texts)
    entries = [
        VectorStoreEntry(id=f"c{i}", content=t, embedding=v, metadata={"source": f"doc-{i}", "document_id": f"doc-{i}"})
        for i, (t, v) in enumerate(zip(texts, vectors))
    ]
    store.add(entries, organization_id="org-rag-test")

    retrieval_service = RetrievalService(embedding_provider=provider, vector_store=store)
    agent = RAGAgent(retrieval_service=retrieval_service)
    context = _make_rag_context()

    result = agent.execute(
        {"query": "support escalation", "top_k": 2, "similarity_threshold": 0.0},
        context,
    )

    assert result.status == AgentExecutionStatus.COMPLETED
    assert result.result.get("context_available") is True
    assert result.result.get("retrieval_count", 0) > 0
    assert result.evidence


def test_rag_agent_empty_query() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    store = InMemoryVectorStore()
    retrieval_service = RetrievalService(embedding_provider=provider, vector_store=store)
    agent = RAGAgent(retrieval_service=retrieval_service)

    result = agent.execute({"query": ""}, _make_rag_context())
    assert result.status == AgentExecutionStatus.FAILED


def test_rag_agent_no_retrieval_service() -> None:
    agent = RAGAgent(retrieval_service=None)
    result = agent.execute({"query": "test"}, _make_rag_context())
    assert result.status == AgentExecutionStatus.FAILED


def test_rag_agent_insufficient_context() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    store = InMemoryVectorStore()
    retrieval_service = RetrievalService(embedding_provider=provider, vector_store=store)
    agent = RAGAgent(retrieval_service=retrieval_service)

    # Query with no data in the store
    result = agent.execute(
        {"query": "quantum computing", "similarity_threshold": 0.99},
        _make_rag_context(),
    )
    assert result.status == AgentExecutionStatus.COMPLETED
    assert result.result.get("context_available") is False
    assert result.result.get("retrieval_count") == 0


def test_rag_agent_citations_present() -> None:
    provider = DeterministicEmbeddingProvider(dimension=64)
    store = InMemoryVectorStore()

    vec = provider.embed_text("policy guideline")
    store.add(
        [VectorStoreEntry(id="c1", content="Policy guideline text", embedding=vec, metadata={"source": "policy-doc", "document_id": "doc-1"})],
        organization_id="org-rag-test",
    )

    retrieval_service = RetrievalService(embedding_provider=provider, vector_store=store)
    agent = RAGAgent(retrieval_service=retrieval_service)
    result = agent.execute(
        {"query": "policy guideline", "similarity_threshold": 0.0},
        _make_rag_context(),
    )

    assert result.result.get("citations")
    assert result.evidence
