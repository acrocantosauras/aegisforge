# Engineering Architecture Requirements (Acme Systems)

Owner: Platform Architecture Guild
Status: Approved — revision 2026-03
Classification: Internal

## AR-1. Deployment topology

The regulated data plane MUST run inside Acme-owned on-premises datacenters.
Managed SaaS control planes are prohibited for any component that stores,
transforms, or routes customer data. Vendor-managed SaaS is permitted only for
non-production sandboxes and only with Platform Architecture sign-off.

An air-gapped installation option is required. Any candidate platform that
cannot be installed on-premises and disconnected from public networks is
non-compliant with AR-1 and cannot proceed to pilot.

## AR-2. Latency budget

End-to-end ingest latency must stay at or below 250 ms at p99 under the
production peak of 90,000 events per second. Sustained throughput requirement is
120,000 events per second with no back-pressure loss.

## AR-3. Integration surface

The platform must integrate with the existing estate without a rip-and-replace:
Apache Kafka 3.6 event bus, PostgreSQL 15 operational store, and the internal
schema registry. Connectors must be based on open, documented protocols.
A proprietary query language that clients must adopt is treated as a lock-in
risk under the Technology Preference Standard.

## AR-4. Operability

Every administrative action must be auditable and reversible. The platform must
expose a documented rollback path for schema evolution and a dry-run mode for
pipeline changes.