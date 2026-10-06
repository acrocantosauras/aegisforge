# Data Platform Procurement Evaluation Notes (Acme Systems)

Owner: Infrastructure Engineering
Status: Working notes — 2026-04
Classification: Internal

These notes record what the infrastructure team believes about the two candidate
platforms. They are internal opinion gathered from the vendor briefings and are
known to be incomplete; where they disagree with a vendor brief, the
Architecture Review Board requires the disagreement to be resolved before a
pilot is approved.

## Northwind Streamline

The team believes Northwind is the lower-risk option operationally because the
vendor operates the platform and Acme staff would not need administrator
credentials. Northwind published p99 latency of 180 ms, which the team believes
comfortably meets the 250 ms requirement.

Open concerns recorded by the team:

- Northwind Streamline cannot be deployed on-premises. This appears to conflict
  with AR-1 and with DG-1 data residency, because the UK/EEA hosting commitment
  is a vendor promise rather than a contractual control that Acme can verify.
- Vendor-managed encryption keys conflict with SB-1 and SB-4.
- The team has not confirmed whether Northwind's tenant isolation can be
  verified independently, which DG-4 requires for Tier-1 data.
- Published pricing of GBP 640,000 plus GBP 190,000 premium support exceeds the
  GBP 1,850,000 annual cost ceiling only when combined with infrastructure
  growth the team has not yet costed.

## Helios Fabric

The team believes Helios meets the on-premises and air-gapped requirements, but
flagged the following as unresolved:

- Helios published p99 latency of 240 ms leaves only 10 ms of headroom against
  the 250 ms requirement in AR-2. The team has not validated this under the
  production peak load of 90,000 events per second.
- The published sub-processor register lists two providers outside the UK/EEA,
  which the team believes requires an explicit legal-entity exception under
  DG-3 even though the software itself is deployed on-premises.
- Helios pricing is per-node, so the team cannot yet project the three-year
  total cost of ownership required by IC-4.

## Recommendation status

No recommendation has been agreed. The Architecture Review Board has asked for a
structured comparison of both platforms against AR-1 through AR-4, SB-1 through
SB-5, DG-1 through DG-5, and IC-1 through IC-5, with every claim traced to a
source document and every conflict between vendor claims and internal
requirements stated explicitly.