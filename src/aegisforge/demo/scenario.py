"""Flagship demo scenario (Phase 10).

One canonical enterprise question, one synthetic corpus, one deterministic
seed.  Nothing here bypasses the orchestration engine: the demo request is
submitted through the normal API and decomposed by the real planner, the real
agents retrieve through the real RAG pipeline, and the real evaluator scores the
real result.

Scenario
--------
Acme Systems must decide whether to replace its self-managed data processing
platform with a commercial streaming platform.  Two candidates have been
briefed (Northwind Streamline, Helios Fabric).  The decision requires external
vendor research, retrieval of private internal requirements and policies,
comparison against those requirements, explicit conflict surfacing, and a
recommendation with evidence.

The request text is deliberately written so the *existing* deterministic planner
recognises its compound ``enterprise knowledge research`` shape.  That produces
the dependency graph the flagship demo is meant to show:

    research (knowledge.search tool)  ┐
                                      ├→ analysis → synthesis
    rag (tenant document retrieval)   ┘

No planner rule was added or special-cased for the demo.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

# --- The canonical demo request -------------------------------------------------
#
# Submitted verbatim from the UI (/execute) or the API.  Wording is chosen for
# the planner's recognised compound-intent shape and to name the requirements
# the corpus actually contains.

FLAGSHIP_DEMO_REQUEST = (
    "Conduct enterprise knowledge research on the Acme Systems data platform "
    "procurement decision: compare Northwind Streamline and Helios Fabric "
    "against our internal architecture, security, data governance, "
    "infrastructure and cost requirements, identify the conflicts between "
    "vendor claims and internal policy, and synthesize a recommendation."
)

#: Shorter label used in docs/UI copy.
FLAGSHIP_DEMO_TITLE = "Acme Systems data platform procurement evaluation"

#: The keyword shape the deterministic planner recognises for this request.
FLAGSHIP_INTENT_SHAPE = "enterprise knowledge research"

#: Expected agent types in the resulting dependency graph (measured, asserted
#: by tests — the demo does not assume them, it verifies them).
FLAGSHIP_EXPECTED_AGENT_TYPES = ("research", "rag", "analysis", "synthesis")


@dataclass(frozen=True)
class DemoDocument:
    """One file in the synthetic demo corpus."""

    #: Stable slug — part of the deterministic document id (idempotent seeding).
    slug: str
    #: Relative path under ``demo/data``.
    relative_path: str
    #: Human title stored on the document row.
    title: str
    #: Category grouping, mirroring the directory layout.
    category: str
    #: Markdown title line, used by tests to assert the corpus is real.
    doc_title: str


#: The synthetic corpus.  Deliberately small and high quality: internal
#: requirements (with explicit, numbered, quotable obligations) plus vendor
#: briefs that genuinely conflict with them.
DEMO_DOCUMENTS: tuple[DemoDocument, ...] = (
    DemoDocument(
        slug="engineering-architecture-requirements",
        relative_path="company-requirements/engineering-architecture-requirements.md",
        title="Engineering Architecture Requirements (Acme Systems)",
        category="company-requirements",
        doc_title="Engineering Architecture Requirements (Acme Systems)",
    ),
    DemoDocument(
        slug="security-baseline",
        relative_path="company-requirements/security-baseline.md",
        title="Security Baseline (Acme Systems)",
        category="company-requirements",
        doc_title="Security Baseline (Acme Systems)",
    ),
    DemoDocument(
        slug="data-governance-policy",
        relative_path="policies/data-governance-policy.md",
        title="Data Governance Policy (Acme Systems)",
        category="policies",
        doc_title="Data Governance Policy (Acme Systems)",
    ),
    DemoDocument(
        slug="infrastructure-cost-constraints",
        relative_path="policies/infrastructure-cost-constraints.md",
        title="Infrastructure and Cost Constraints (Acme Systems)",
        category="policies",
        doc_title="Infrastructure and Cost Constraints (Acme Systems)",
    ),
    DemoDocument(
        slug="current-platform-architecture",
        relative_path="architecture/current-platform-architecture.md",
        title="Current Data Platform Architecture (Acme Systems)",
        category="architecture",
        doc_title="Current Data Platform Architecture (Acme Systems)",
    ),
    DemoDocument(
        slug="technology-preference-standard",
        relative_path="architecture/technology-preference-standard.md",
        title="Technology Preference Standard (Acme Systems)",
        category="architecture",
        doc_title="Technology Preference Standard (Acme Systems)",
    ),
    DemoDocument(
        slug="northwind-streamline-vendor-brief",
        relative_path="product/northwind-streamline-vendor-brief.md",
        title="Northwind Streamline — Vendor Capability Brief",
        category="product",
        doc_title="Northwind Streamline — Vendor Capability Brief",
    ),
    DemoDocument(
        slug="helios-fabric-vendor-brief",
        relative_path="product/helios-fabric-vendor-brief.md",
        title="Helios Fabric — Vendor Capability Brief",
        category="product",
        doc_title="Helios Fabric — Vendor Capability Brief",
    ),
    DemoDocument(
        slug="procurement-evaluation-notes",
        relative_path="product/procurement-evaluation-notes.md",
        title="Data Platform Procurement Evaluation Notes (Acme Systems)",
        category="product",
        doc_title="Data Platform Procurement Evaluation Notes (Acme Systems)",
    ),
)

#: Stable prefix on every seeded demo document id, so reset/cleanup can find
#: them without depending on titles or chunk ids.
DEMO_DOCUMENT_ID_PREFIX = "demo"


def repo_root() -> Path:
    """Repository root (the directory containing ``demo/``)."""
    return Path(__file__).resolve().parents[3]


def demo_data_dir() -> Path:
    """Absolute path to ``demo/data``."""
    return repo_root() / "demo" / "data"


def document_id_for(slug: str, owner_id: str) -> str:
    """Deterministic, owner-scoped document id for a demo document.

    Two properties matter:

    * **Deterministic** — keyed on (owner, slug) so re-running the seeder for
      the same account always resolves to the same row, which the seeder uses
      to skip work that is already done.
    * **Owner-scoped** — ``documents.id`` is the primary key, so two accounts
      that seed the same corpus (self-registration shares the default
      organization) would otherwise collide.  Each account gets its own copy of
      the demo corpus, which matches retrieval's owner-scoped authorization.
    """
    owner_tag = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:8]
    return f"{DEMO_DOCUMENT_ID_PREFIX}-{owner_tag}-{slug}"


def read_demo_document(document: DemoDocument) -> bytes:
    """Read a demo corpus file as bytes (the ingestion pipeline's input)."""
    path = demo_data_dir() / document.relative_path
    return path.read_bytes()


def demo_id_prefix() -> str:
    """Prefix shared by every seeded demo document id."""
    return f"{DEMO_DOCUMENT_ID_PREFIX}-"