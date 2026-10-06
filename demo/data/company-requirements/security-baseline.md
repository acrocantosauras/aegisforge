# Security Baseline (Acme Systems)

Owner: Security Engineering
Status: Approved — revision 2026-01
Classification: Internal

## SB-1 Transport and encryption

All service-to-service traffic must use mutual TLS 1.3. Data at rest must be
encrypted with customer-managed keys held in the internal key management
service. Vendor-managed keys are not acceptable for regulated data classes.

## SB-2 Assurance evidence

A candidate platform must provide a current SOC 2 Type II report, ISO 27001
certification covering the same product scope, and a penetration test summary
from the last twelve months. Certifications that cover a different product tier
than the one being adopted do not satisfy this requirement.

## SB-3 Access control

Role-based access control with per-environment separation is mandatory.
Break-glass accounts must be disabled by default, time-bound, and logged.
Single sign-on through the corporate identity provider is required.

## SB-4 Customer-managed key support

Where a vendor offers customer-managed keys, key rotation must be performed by
Acme Systems without vendor involvement, and rotation events must be exportable
to the security data lake.

## SB-5 Non-negotiable controls

Encryption, RBAC, SSO, and audit-log export are non-negotiable. A platform that
cannot meet SB-1 through SB-4 is not eligible for adoption regardless of
functional fit or cost.