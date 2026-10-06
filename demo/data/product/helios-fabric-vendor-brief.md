# Helios Fabric — Vendor Capability Brief (Synthetic Reference Material)

Publisher: Helios Fabric Ltd (fictional vendor)
Document type: Vendor-published product brief
Revision: 2.7 — 2025

> Reference material only. Acme Systems has not verified these claims.

## Deployment model

Helios Fabric is available as self-managed software running on Kubernetes
clusters operated by the customer. Air-gapped installation is supported and is
covered by the published installation guide. Deployment regions are selected by
the customer; the vendor does not operate a hosted service in the UK.

## Performance

Published figures: p99 ingest latency of 240 ms at 120,000 events per second.
The vendor states that the streaming engine is "tuned for the Kafka connector
path" and that latency targets above 120,000 events per second are not
committed.

## Integrations

Native connectors for Apache Kafka 3.x and PostgreSQL 15 (read and write).
Applications are written against the vendor's "Helios Pipeline Definition"
(YAML-based); SQL is available only through a commercial SQL-connector add-on.
The vendor publishes a formal API compatibility promise for the Kafka and
PostgreSQL connectors.

## Security and assurance

The vendor publishes an ISO 27001 certificate and provides an on-request
penetration test summary from the most recent annual assessment. Customer-managed
keys via the internal key management service are supported through a Helm
values configuration. Single sign-on is supported via OIDC. Audit logs are
written to a customer-managed append-only store with an export manifest.

## Commercials

Published list pricing: GBP 310,000 per year for the Platform licence, plus
GBP 120,000 per year for premium support, exclusive of infrastructure. Pricing
is per-node and does not scale with event volume. Price escalation is capped at
5 percent.

## Contractual residency and retention

Data residency is determined by where the customer deploys the software;
sub-processors used by the vendor for support are listed in the published
sub-processor register, which includes two providers outside the UK/EEA.
Retention is operator-configured; the published default is 90 days.