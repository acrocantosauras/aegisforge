"""In-container RAG retrieval probe.

Does an org-scoped search return User A's uploaded chunk when queried with
the shared default-org scope (exactly what User B's RAG agent would use)?
Runs INSIDE the api container with the real settings/vector store.
"""
from __future__ import annotations

import sys

SECRET = "BANANA-FALCON-9931"


def main() -> int:
    from aegisforge.config import get_settings
    from aegisforge.rag.embeddings import get_embedding_provider
    from aegisforge.rag.vector_store import get_vector_store

    settings = get_settings()
    print("db:", settings.database_url.split("@")[-1])
    print("embedding provider:", settings.embedding_provider)

    provider = get_embedding_provider(
        settings.embedding_provider,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        batch_size=settings.embedding_batch_size,
    )
    store = get_vector_store(
        "pgvector" if settings.database_url.startswith("postgresql") else "memory",
        db_session_factory=lambda: __import__(
            "aegisforge.db.session", fromlist=["get_session_factory"]
        ).get_session_factory(settings)(),
        dimension=settings.embedding_dimension,
    )
    print("store type:", type(store).__name__)

    # Count what the shared default-org scope can see
    total = store.count("default-org")
    print("chunks visible under 'default-org' scope:", total)

    # Search for the secret phrase under the shared org scope
    vec = provider.embed_texts([f"vault access code {SECRET}"])[0]
    hits = store.search(vec, top_k=5, organization_id="default-org", similarity_threshold=0.0)
    print("search hits:", len(hits))
    leaked = False
    for h in hits[:5]:
        content = getattr(h, "content", "") or ""
        marker = SECRET in content
        leaked = leaked or marker
        print(
            "  hit:",
            getattr(h, "id", "?"),
            "| secret present:",
            marker,
            "| snippet:",
            content[:80].replace("\n", " "),
        )
    print("\nRESULT:", "LEAK (B could retrieve A's chunk)" if leaked else "NO LEAK at vector layer")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
