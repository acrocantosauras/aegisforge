# Data Governance Policy (Acme Systems)

Owner: Data Governance Office
Status: Approved — revision 2025-11
Classification: Internal

## DG-1 Data residency

Customer personal data must remain inside the United Kingdom and European
Economic Area at all times, including backups, logs, and support diagnostics.
Any component that replicates data to a region outside the UK/EEA is
non-compliant. Residency commitments must be contractual, not best-effort.

## DG-2 Retention and deletion

Regulated event data has a seven-year retention requirement. Deletion requests
must be honoured within thirty days. A platform that cannot demonstrate
configurable retention with customer-controlled deletion windows does not meet
this obligation.

## DG-3 Third-party processing

Any third party processing personal data requires a signed data processing
agreement, a documented sub-processor list, and prior approval from the Data
Governance Office. Sub-processors located outside the UK/EEA require an
explicit legal-entity exception.

## DG-4 Data classification

Tier-1 classified data (customer personal data, payment metadata, credentials)
requires encryption in transit and at rest, tenant-scoped access control, and a
full audit trail. Tier-1 data may not be stored in a shared tenancy that permits
cross-tenant visibility.

## DG-5 Audit evidence

Audit records must be exportable to the enterprise security data lake within
twenty-four hours of an administrative action, and must be retained for seven
years. Platforms whose audit export is manual-only are considered a material
control weakness under this policy.