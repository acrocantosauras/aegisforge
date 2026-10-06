# AegisForge Flagship Demo

One canonical enterprise decision, run end-to-end through the real platform:
planner → parallel agents → tenant-scoped RAG → structured analysis → grounded
synthesis → measured evaluation.

Everything here is synthetic. There is no copyrighted material, no real company,
and no live web scraping.

## The scenario

**Acme Systems** (fictional) must decide whether to replace its self-managed
data processing platform with a commercial streaming platform. Two candidates
have been briefed (Northwind Streamline, Helios Fabric). Answering the question
requires reading internal requirements, policies, and cost constraints,
retrieving vendor material, comparing the two against every requirement,
surfacing the conflicts, and producing a recommendation with evidence.

This is deliberately not a question a single LLM call can answer: it needs
private knowledge, structured comparison, and an auditable trail.

## Layout

```
demo/
  README.md                     ← this file
  data/
    company-requirements/        ← architecture + security requirements (internal)
    policies/                    ← data governance + infrastructure/cost constraints
    architecture/                ← current estate + technology preference standard
    product/                     ← two vendor briefs + internal evaluation notes
```

The corpus is small on purpose (9 documents, ~15 KB). It is written so that the
agents must actually retrieve and reason: requirements are numbered and quotable
(AR-1, SB-4, DG-3, IC-4 …), and the vendor briefs genuinely contradict them.

### The conflicts the demo surfaces

| Internal requirement | Vendor claim | Conflict |
|---|---|---|
| AR-1: regulated data plane must run on-premises, air-gapped | Northwind is hosted-only, multi-tenant; air-gapped is "on the roadmap" | **Blocking** |
| SB-1/SB-4: customer-managed encryption keys | Northwind uses vendor-managed keys | **Blocking** |
| AR-2: ≤ 250 ms p99 ingest | Helios publishes 240 ms p99 (10 ms headroom) | At risk |
| IC-4: ≤ £1.85M/yr run rate | Northwind £640k + £190k support, before infrastructure | At risk |
| TP-1: open protocols | Helios requires a proprietary YAML pipeline language | Lock-in |

The platform does not resolve these for you: it retrieves both sides, reports
the conflicts, and shows how confident it is in the evidence.

## The canonical request

Submit this from **Execute** (it is the highlighted "Flagship demo" example):

```
Conduct enterprise knowledge research on the Acme Systems data platform
procurement decision: compare Northwind Streamline and Helios Fabric against
our internal architecture, security, data governance, infrastructure and cost
requirements, identify the conflicts between vendor claims and internal policy,
and synthesize a recommendation.
```

The wording is chosen so the *existing* deterministic planner recognises its
compound `enterprise knowledge research` shape. No planner rule was added,
special-cased, or bypassed for the demo. See
[`docs/phase10-flagship-demo.md`](../docs/phase10-flagship-demo.md) for the
resulting task graph and the full walkthrough.

## Seed the knowledge base

The demo corpus must be ingested **as a specific user**, because retrieval is
owner-scoped: an account can only retrieve documents it ingested itself.

```bash
# 1. Create the account (UI sign-up, or:)
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com","password":"StrongPass123!","full_name":"Demo Operator"}'

# 2. Seed the demo corpus for that account
DATABASE_URL="postgresql+psycopg://aegisforge:aegisforge@localhost:5432/aegisforge" \
  python scripts/seed_demo.py --email demo@example.com
```

The seeder uses the **real ingestion pipeline** — the same extractor, chunker,
embedding provider, vector store, and owner-scoping rules as uploading a file
through the Knowledge page. It is safe to run repeatedly: unchanged documents
are skipped, never duplicated.

```
  engineering-architecture-requirements   created     4 chunk(s)
  security-baseline                       created     4 chunk(s)
  ...
9 demo document(s): 9 created, 0 updated, 0 unchanged
All demo documents are indexed and retrievable by this user.
```

Preview without writing: `--dry-run`. Reset: `scripts/reset_demo.py --email …`
(or `--reset` on the seeder).

## Run the demo

1. Sign in as the seeded account.
2. Open **Execute**, click the flagship example, **Submit & Execute**.
3. Watch the Execution Workspace: 4 tasks in 3 waves (research ∥ RAG →
   analysis → synthesis), real tool activity, retrieved evidence, the
   evaluation grid, and a grounded final report.

Refresh at any point — the workspace restores from durable state.

**Deterministic under the configured deterministic embedding provider.** With
the default local provider (`EMBEDDING_PROVIDER=deterministic`), the
deterministic planner, and the same corpus, the run repeats exactly: same
citations, same conflict count, same evaluation score. That reproducibility is a
property of those settings, not a universal guarantee — configure a remote
embedding provider (e.g. `EMBEDDING_PROVIDER=openai`) and the vectors change, so
retrieval ranking, citations, conflict counts and evaluation scores can all
differ.

## Notes

- The seeder never creates users, never changes roles, and never writes outside
  the target user's organization + owner scope.
- Demo documents are prefixed (`demo-<owner>-<slug>`) so a reset can find them
  without touching anything you uploaded yourself.
- If you seed the demo and then run the request as a *different* account, the
  RAG leg correctly finds nothing and the run reports insufficient context.
  That is isolation working, not a bug.