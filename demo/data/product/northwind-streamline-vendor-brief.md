# Northwind Streamline — Vendor Capability Brief (Synthetic Reference Material)

Publisher: Northwind Data Systems (fictional vendor)
Document type: Vendor-published product brief
Revision: 4.2 — 2026

> Reference material only. Acme Systems has not verified these claims; under
> the Technology Preference Standard (TP-5) vendor claims are unverified
> marketing input until confirmed by contract or a controlled pilot.

## Deployment model

Northwind Streamline is delivered exclusively as a vendor-hosted multi-tenant
service. There is no on-premises, private-cloud, or air-gapped edition. The
vendor states that an air-gapped deployment option is "on the roadmap for a
future release" and is not committed to any date. Regional deployment is
available in the United Kingdom (London) and European Economic Area (Ireland,
Frankfurt).

## Performance

Published figures: p99 ingest latency of 180 ms under a 150,000 events per
second sustained load; horizontal scaling without rebalancing pauses. Figures
are measured on the vendor's own benchmark with synthetic workloads.

## Integrations

Native connectors for Apache Kafka (read and write) and PostgreSQL 15
(read-only replica). The vendor's own "Stream Query Language" (SQL) is required
for all application-level stream logic. The vendor states that third-party
connectors are "supported on a best-effort basis".

## Security and assurance

SOC 2 Type II and ISO 27001 certifications are held for the hosted service.
Encryption at rest uses vendor-managed keys; customer-managed keys are described
as a roadmap item. Single sign-on is supported via SAML 2.0 and OIDC. Audit logs
are retained for 400 days and are exportable to a customer-managed bucket via
the vendor console or a scheduled export job.

## Commercials

Published list pricing: GBP 640,000 per year for the Enterprise tier at the
committed throughput described above, excluding premium support. Premium 24x7
support with a named technical account manager is an additional GBP 190,000 per
year. Annual price escalation is capped at 7 percent.

## Contractual residency and retention

The published data processing agreement commits to UK/EEA processing for
customer data. Retention of ingested event data is configurable between 30 days
and 400 days. Data deletion requests are processed within 60 days.