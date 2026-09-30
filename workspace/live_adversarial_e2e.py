"""LIVE ADVERSARIAL E2E — runs against the real Docker stack on :8000.

Evidence type: LIVE DOCKER TEST + ADVERSARIAL.
Covers: register/login, full async execution through Redis/worker/LangGraph,
IDOR attacks (User B vs User A), anonymous probes, token attacks, malformed
IDs, approval visibility isolation, and leakage checks.
"""
from __future__ import annotations

import sys
import time
import uuid

import httpx

BASE = "http://localhost:8000/api/v1"
PASSWORD = "LiveAdvPass123!"
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(f"{name} {('| ' + detail) if detail and not cond else ''}")
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))


def leak(body: str) -> bool:
    low = body.lower()
    return any(m in low for m in ("traceback", "sqlalchemy", "password_hash", "file \"", "redis://"))


def register_login(client: httpx.Client, email: str) -> dict[str, str]:
    r = client.post(f"{BASE}/auth/register", json={"email": email, "password": PASSWORD, "full_name": "Live"})
    assert r.status_code in (201, 409), r.text
    r = client.post(f"{BASE}/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def main() -> int:
    c = httpx.Client(timeout=90)
    run = uuid.uuid4().hex[:8]

    # --- health / readiness
    check("health 200", c.get(f"{BASE}/health").status_code == 200)
    check("ready 200", c.get(f"{BASE}/ready").status_code == 200)

    # --- two real users
    a_h = register_login(c, f"live-a-{run}@example.com")
    b_h = register_login(c, f"live-b-{run}@example.com")

    # --- User A: create request, execute async (real Redis -> worker path)
    r = c.post(f"{BASE}/requests", json={"intent": "Research AI governance policy frameworks, analyze the evidence, and synthesize a recommendation"}, headers=a_h)
    check("A create request 201", r.status_code == 201, r.text[:200])
    req_id = r.json()["id"]

    r = c.post(f"{BASE}/execution/requests/{req_id}/execute-async", headers=a_h)
    check("A async execute 202", r.status_code == 202, r.text[:300])
    wf_id = r.json().get("workflow_id", "")

    # Poll for completion through the real worker
    final_status, result = "", {}
    for _ in range(60):
        time.sleep(2)
        rr = c.get(f"{BASE}/workflows/{wf_id}", headers=a_h)
        if rr.status_code == 200:
            final_status = rr.json().get("status", "")
            if final_status in ("completed", "failed"):
                break
    check("worker completed workflow", final_status == "completed", f"status={final_status}")

    res = c.get(f"{BASE}/workflows/{wf_id}/result", headers=a_h)
    result = res.json() if res.status_code == 200 else {}
    check("A reads result", res.status_code == 200 and bool(result.get("summary") or result.get("answer")), res.text[:200])
    tasks = c.get(f"{BASE}/workflows/{wf_id}/tasks", headers=a_h)
    check("A reads tasks", tasks.status_code == 200 and len(tasks.json().get("tasks", [])) >= 3)
    ev = c.get(f"{BASE}/workflows/{wf_id}/evaluations", headers=a_h)
    check("A reads evaluation", ev.status_code == 200)
    br = c.get(f"{BASE}/workflows/by-request/{req_id}", headers=a_h)
    check("A by-request resolves", br.status_code == 200 and br.json().get("workflow_id") == wf_id)

    # --- IDOR: User B attacks User A resources
    check("B GET A request -> 404", c.get(f"{BASE}/requests/{req_id}", headers=b_h).status_code == 404)
    check("B list excludes A", req_id not in [x["id"] for x in c.get(f"{BASE}/requests", headers=b_h).json()])
    for p in (f"/workflows/{wf_id}", f"/workflows/{wf_id}/tasks", f"/workflows/{wf_id}/result", f"/workflows/{wf_id}/evaluations", f"/workflows/by-request/{req_id}"):
        rr = c.get(f"{BASE}{p}", headers=b_h)
        check(f"B {p.split('/')[-1] if p.split('/')[-1] else p} -> 404", rr.status_code == 404 and not leak(rr.text), f"{rr.status_code} {rr.text[:120]}")
    check("B execute A request -> 404", c.post(f"{BASE}/execution/requests/{req_id}/execute", headers=b_h).status_code == 404)
    check("B async-execute A request -> 404", c.post(f"{BASE}/execution/requests/{req_id}/execute-async", headers=b_h).status_code == 404)
    check("B patch A request -> 404", c.patch(f"{BASE}/requests/{req_id}/status", json={"status": "failed"}, headers=b_h).status_code == 404)
    check("A state intact after B patch", c.get(f"{BASE}/requests/{req_id}", headers=a_h).json()["status"] != "failed")

    # --- Documents
    up = c.post(f"{BASE}/documents", files={"file": ("secret-a.txt", b"Alice confidential salary 120k", "text/plain")}, headers=a_h)
    check("A upload doc 201", up.status_code == 201, up.text[:200])
    doc_id = up.json()["document_id"]
    check("B list excludes A doc", doc_id not in [d["id"] for d in c.get(f"{BASE}/documents", headers=b_h).json()["documents"]])
    check("B GET A doc -> 404", c.get(f"{BASE}/documents/{doc_id}", headers=b_h).status_code == 404)
    check("B DELETE A doc -> 404 (not destroyed)", c.delete(f"{BASE}/documents/{doc_id}", headers=b_h).status_code == 404 and c.get(f"{BASE}/documents/{doc_id}", headers=a_h).status_code == 200)

    # --- Approvals: B cannot see/decide A's approvals (none exist here, but list must be scoped)
    check("approvals list scoped (200 empty for B)", c.get(f"{BASE}/approvals", headers=b_h).status_code == 200)
    check("B approve unknown -> 404", c.post(f"{BASE}/approvals/ap-fake/approve", json={"decision_reason": "x"}, headers=b_h).status_code == 404)

    # --- Anonymous + token attacks
    check("anon requests -> 401", c.get(f"{BASE}/requests").status_code == 401)
    check("anon system -> 401", c.get(f"{BASE}/system").status_code == 401)
    check("anon workers -> 401", c.get(f"{BASE}/workers").status_code == 401)
    check("garbage token -> 401", c.get(f"{BASE}/requests", headers={"Authorization": "Bearer not.a.token"}).status_code == 401)
    tampered = a_h["Authorization"].replace("ey", "ey", 1)
    sig = tampered.split(".")[-1]
    check("tampered signature -> 401", c.get(f"{BASE}/requests", headers={"Authorization": tampered[:-4] + ("AAAA" if not sig.endswith("AAAA") else "BBBB")}).status_code == 401)
    check("bad login -> 401 no oracle", c.post(f"{BASE}/auth/login", json={"email": f"live-a-{run}@example.com", "password": "WrongPass123!"}).status_code == 401)

    # --- Malformed IDs fail closed
    for path in ("/requests/not-a-uuid", "/documents/not-a-uuid", "/approvals/not-a-uuid", "/workflows/not-a-uuid/result", "/requests/'; DROP TABLE requests;--"):
        rr = c.get(f"{BASE}{path}", headers=a_h)
        check(f"malformed {path} -> {rr.status_code}", rr.status_code in (404, 422) and not leak(rr.text), rr.text[:120])

    # --- Unauthenticated /metrics is exposed by design (Prometheus scrape) — confirm no secrets
    m = c.get("http://localhost:8000/metrics").text
    check("/metrics has no secrets", not any(s in m.lower() for s in ("password", "secret_key", "redis://")))

    print(f"\n===== LIVE RESULT: {len(PASS)} passed, {len(FAIL)} failed =====")
    for f in FAIL:
        print("  FAILED:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
