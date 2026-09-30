"""Fishing-attack probe (in-container, real settings).

User B knows nothing about A's document. B fishes with broad/common topic
terms at the DEFAULT similarity threshold (0.3, what RAGAgent uses). Any
hit containing A's content = exploitable cross-user leak through the real
retrieval layer.
"""
from __future__ import annotations

import sys

SECRET = "BANANA-FALCON-9931"
ZEBRA = "ZEBRA-QUARTZ-7741-CONFIDENTIAL"


def main() -> int:
    from aegisforge.config import get_settings
    from aegisforge.db.session import get_session_factory
    from aegisforge.rag.embeddings import get_embedding_provider
    from aegisforge.rag.vector_store import get_vector_store

    settings = get_settings()
    provider = get_embedding_provider(
        settings.embedding_provider,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        batch_size=settings.embedding_batch_size,
    )
    store = get_vector_store(
        "pgvector" if settings.database_url.startswith("postgresql") else "memory",
        db_session_factory=lambda: get_session_factory(settings)(),
        dimension=settings.embedding_dimension,
    )

    fishing_queries = [
        "salary", "payroll", "confidential salary information",
        "employee compensation details", "vault access",
        "access codes and passwords", "project vault",
        "human resources salary data", "private payroll file",
    ]
    leaked = False
    for q in fishing_queries:
        vec = provider.embed_texts([q])[0]
        hits = store.search(vec, top_k=5, organization_id="default-org", similarity_threshold=0.3)
        for h in hits:
            content = getattr(h, "content", "") or ""
            if SECRET in content or ZEBRA in content or "salary" in content.lower() or "vault" in content.lower():
                leaked = True
                print(f"LEAK via query={q!r} chunk={getattr(h, 'id', '?')} score={getattr(h, 'score', '?')}")
                print("   content:", content[:100].replace("\n", " "))
    if not leaked:
        print("No fishing hit above 0.3 threshold under default-org")
    print("\nRESULT:", "EXPLOITABLE LEAK" if leaked else "not exploitable at default threshold (org-scope design flaw remains)")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
