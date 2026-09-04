# CSRF / Auth Security Decision (Phase 4.2, Part 12)

**Date:** 2026-09-04
**Status:** Accepted — CSRF protection intentionally NOT implemented (not applicable)

## Executive Decision

AegisForge does **not** use browser-authenticated cookies, therefore classic
Cross-Site Request Forgery (CSRF) attacks are **not applicable** to this
architecture. No cookie-CSRF machinery has been added, per the security
principle "correct security, not checking a CSRF box artificially."

## Why CSRF Does Not Apply

CSRF exploits the browser's automatic attachment of ambient credentials
(cookies) to cross-site requests. The attack only works when:

1. The victim's browser holds an authentication cookie for the target site, **and**
2. A malicious page can cause the browser to fire a state-changing request to that site.

AegisForge's authentication does **not** use cookies:

| Mechanism | Status | Detail |
| --------- | ------ | ------ |
| JWT via `Authorization: Bearer` header | ✅ used | Every authenticated API call sends the token explicitly in the `Authorization` header |
| HttpOnly session/cookie | ❌ not used | No cookie is ever set by the API |
| Token persistence | localStorage | The SPA stores the JWT in `localStorage` and injects it via the `ApiClient` |

Because the token is never attached automatically by the browser, a malicious
site cannot cause authenticated requests to fire — **there is no ambient
credential to exploit**. Cross-site requests simply arrive without an
`Authorization` header and are rejected as unauthenticated.

## Security Controls in Place

| Control | Where | Detail |
| ------- | ----- | ------ |
| Restricted CORS | `src/aegisforge/app.py` + `config.cors_origins` | Origins are explicit and configurable (`http://localhost:3000` default); **no wildcard** for authenticated use. `allow_credentials=True` is harmless here because no cookies are used. |
| Bearer-token authz | `auth_service.get_current_user` | Every protected route resolves the user from the JWT; invalid/missing tokens → 401 |
| Tenant isolation | All services | Requests/workflows/jobs/approvals/documents/audit are scoped by `organization_id` (verified in Part 13 security recheck tests) |
| Rate limiting | Redis-backed `RateLimitMiddleware` | Protects auth and API endpoints from abuse (Part 11) |
| Secrets never in client state | Frontend | Only the JWT is stored; no API keys, DB credentials, or passwords reach the browser |

## Token Storage Assessment

The JWT lives in `localStorage` (see `frontend/src/lib/auth.tsx`). This is the
standard tradeoff for SPA bearer-token apps and is acceptable here **provided**
the app has no XSS sink that can read storage. Residual risks and mitigations:

- **XSS exfiltration risk:** a stored-XSS payload could read `localStorage`.
  Mitigations: React's default output escaping, no `dangerouslySetInnerHTML`
  in the codebase, audit events sanitized on the backend (logs page strips
  secrets), and strict Content-Security-Policy recommended in production.
- **No cookie flag hardening exists** because no cookies exist — `HttpOnly`,
  `Secure`, `SameSite` are not applicable.

If AegisForge later introduces cookie-based sessions (e.g., server-side
sessions), this decision must be revisited and `SameSite=Strict`/CSRF tokens
must be added at that time.

## How This Was Verified

- Auth routes (`register`/`login`) return a JSON `access_token`; no
  `Set-Cookie` header exists in the backend (verified by reading
  `src/aegisforge/api/routes/auth.py`).
- Frontend `ApiClient.request()` attaches the token only via the
  `Authorization` header (`frontend/src/lib/api.ts`).
- `get_current_user` dependency enforces bearer auth on protected routes
  (Phase 4.2 security recheck tests cover unauthorized access denial).
- CORS origins are configurable and never `*` for authenticated usage.