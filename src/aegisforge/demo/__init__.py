"""Flagship demo scenario: synthetic corpus, canonical request, deterministic seeding.

This package contains *data and seeding* only.  Execution always goes through
the real orchestration engine — there is no demo code path that short-circuits
planning, agents, retrieval, or evaluation.
"""
from __future__ import annotations

from aegisforge.demo.scenario import (
    DEMO_DOCUMENTS,
    FLAGSHIP_DEMO_REQUEST,
    FLAGSHIP_DEMO_TITLE,
    DemoDocument,
    demo_data_dir,
    document_id_for,
)

__all__ = [
    "DEMO_DOCUMENTS",
    "FLAGSHIP_DEMO_REQUEST",
    "FLAGSHIP_DEMO_TITLE",
    "DemoDocument",
    "demo_data_dir",
    "document_id_for",
]