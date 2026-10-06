# Technology Preference Standard (Acme Systems)

Owner: Architecture Review Board
Status: Approved — revision 2025-09
Classification: Internal

## TP-1 Default to open protocols

Acme Systems prefers platforms built on open, documented protocols (Kafka,
PostgreSQL wire protocol, gRPC, OpenTelemetry). A proprietary query or
configuration language that clients must adopt is scored as a lock-in risk.

## TP-2 Standards alignment

Preferred: Kubernetes operators, OpenTelemetry-compatible telemetry, and
row-level or tenant-scoped access control implemented in the storage engine
rather than in an application proxy.

## TP-3 Deployment locality

A platform that only operates as a vendor-hosted SaaS service is out of
preference for regulated data, and is in conflict with the Engineering
Architecture Requirements (AR-1) and the Data Governance Policy (DG-1).

## TP-4 Substitution and exit

Any new dependency must have a documented substitution path. The Architecture
Review Board requires a written exit plan before a pilot is approved.

## TP-5 Evidence quality

Vendor material is treated as unverified marketing input until it is confirmed
by contract, by independent assurance evidence, or by a controlled pilot. Where
vendor claims conflict with internal requirements, the requirement prevails for
eligibility purposes and the conflict must be recorded in the evaluation.