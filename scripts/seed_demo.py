#!/usr/bin/env python
"""Seed the AegisForge flagship demo knowledge base (Phase 10).

Ingests ``demo/data/**`` into the real RAG pipeline for one user, using that
user's own organization and owner scope.  Safe to run repeatedly: unchanged
documents are skipped, never duplicated.

Usage
-----
    # seed for an existing account
    python scripts/seed_demo.py --email demo@example.com

    # remove previously seeded demo documents, then re-seed
    python scripts/seed_demo.py --email demo@example.com --reset

    # show what would change without writing
    python scripts/seed_demo.py --email demo@example.com --dry-run

The account must already exist (``POST /api/v1/auth/register`` or the sign-up
form).  The script never creates users, never elevates roles, and never writes
outside the given user's organization + owner scope.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from aegisforge.config import Settings  # noqa: E402
from aegisforge.db.models import UserModel  # noqa: E402
from aegisforge.db.session import get_engine, get_session_factory  # noqa: E402
from aegisforge.demo.seed import seed_demo_documents  # noqa: E402
from aegisforge.demo.scenario import DEMO_DOCUMENTS, document_id_for  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the AegisForge flagship demo knowledge base.",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Email of the existing account the demo corpus is seeded for.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete previously seeded demo documents for this user first.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what seeding would do without writing.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    settings = Settings()
    engine = get_engine(settings.database_url)

    from sqlalchemy import inspect

    if not inspect(engine).has_table("users"):
        print(
            "error: the users table does not exist. Start the stack "
            "(docker compose up -d) so migrations run first.",
            file=sys.stderr,
        )
        return 1

    session_factory = get_session_factory(settings)
    db = session_factory()
    try:
        user = (
            db.query(UserModel)
            .filter(UserModel.email == args.email.strip().lower())
            .first()
        )
        if user is None:
            print(
                f"error: no account for {args.email}. Register it first "
                "(the sign-up form or POST /api/v1/auth/register).",
                file=sys.stderr,
            )
            return 1

        print(f"user      : {user.email}")
        print(f"org       : {user.organization_id}")
        print(f"owner     : {user.id}")
        print(f"documents : {len(DEMO_DOCUMENTS)}")
        print(f"database  : {os.environ.get('DATABASE_URL', settings.database_url).split('@')[-1]}")
        print()

        if args.dry_run:
            # Report existing state without writing.
            from aegisforge.demo.seed import _expected_content_hash, _existing_document

            for document in DEMO_DOCUMENTS:
                existing = _existing_document(
                    db,
                    document_id_for(document.slug, user.id),
                    user.organization_id,
                    user.id,
                )
                expected = _expected_content_hash(
                    document, settings, document_id_for(document.slug, user.id)
                )
                if existing is None:
                    state = "would create"
                elif existing.content_hash == expected:
                    state = "unchanged (skipped)"
                else:
                    state = "would update"
                print(f"  {document.slug:<38} {state}")
            print("\ndry run — nothing was written.")
            return 0

        outcome = seed_demo_documents(
            db,
            organization_id=user.organization_id,
            owner_id=user.id,
            settings=settings,
            reset=args.reset,
        )

        for document in DEMO_DOCUMENTS:
            slug = document.slug
            if slug in outcome.created:
                state = "created"
            elif slug in outcome.updated:
                state = "updated"
            else:
                state = "unchanged"
            print(f"  {slug:<38} {state:<10} {outcome.chunk_counts.get(slug, 0):>3} chunk(s)")

        print()
        print(outcome.summary())
        if outcome.indexing_failures:
            print(
                "warning: vector indexing failed for: "
                + ", ".join(outcome.indexing_failures),
                file=sys.stderr,
            )
            print(
                "  documents and chunks were persisted, but RAG retrieval will "
                "not see them until indexing succeeds.",
                file=sys.stderr,
            )
            return 2
        print("All demo documents are indexed and retrievable by this user.")
        print()
        print("Next: sign in as this user and submit the canonical demo request")
        print("from the Execute page (or see docs/phase10-flagship-demo.md).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())