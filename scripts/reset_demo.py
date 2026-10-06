#!/usr/bin/env python
"""Remove the seeded AegisForge flagship demo knowledge base for one user (Phase 10).

Deletes only documents whose ids start with ``demo-`` **and** which belong to
the given user's organization and owner.  Chunk rows and vector embeddings are
removed with the same authorization predicate, so no orphaned chunks remain.

Usage:
    python scripts/reset_demo.py --email demo@example.com

Requires the account to already exist (the script never creates users and never
changes roles).  Documents uploaded outside the demo corpus are untouched.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from aegisforge.config import Settings
from aegisforge.db.models import UserModel
from aegisforge.db.session import get_session_factory
from aegisforge.demo.seed import delete_demo_documents


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove seeded AegisForge flagship demo documents for one user.",
    )
    parser.add_argument("--email", required=True, help="Email of the account to reset.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings = Settings()
    db = get_session_factory(settings)()
    try:
        user = (
            db.query(UserModel)
            .filter(UserModel.email == args.email.strip().lower())
            .first()
        )
        if user is None:
            print(f"error: no account for {args.email}", file=sys.stderr)
            return 1

        removed = delete_demo_documents(
            db,
            organization_id=user.organization_id,
            owner_id=user.id,
            settings=settings,
        )
        print(
            f"Removed {removed} demo document(s) for {user.email} "
            f"(organization {user.organization_id})."
        )
        if removed == 0:
            print("Nothing to reset — no seeded demo documents for this account.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())