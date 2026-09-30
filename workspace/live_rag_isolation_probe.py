"""LIVE RAG TENANT-ISOLATION PROBE v2 — no secret token in B's query.

A ingests a document containing a unique secret phrase. B runs a workflow
whose intent mentions only the TOPIC (never the secret). If B's retrieved
citations/content contain the secret phrase, that's a real cross-tenant
retrieval leak — B could not have known the phrase.
"""
from __future__ import annotations

import sys
import time
import uuid

import httpx

BASE = "http://localhost:8000/api/v1"
PASSWORD = "RagIsoPass123!"
SECRET = "BANANA-FALCON-9931"


def register_login(c: httpx.Client, email: str) -> dict[str, str]:
    c.post(f"{BASE}/auth/register", json={"email": email, "password": PASSWORD, "full_name": "R"})
    r = c.post(f"{BASE}/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def run_workflow(c: httpx.Client, headers: dict, intent: str) -> tuple[str, dict, dict]:
    r = c.post(f"{BASE}/requests", json={"intent": intent}, headers=headers)
    assert r.status_code == 201, r.text
    req_id = r.json()["id"]
    ex = c.post(f"{BASE}/execution/requests/{req_id}/execute-async", headers=headers)
    assert ex.status_code == 202, ex.text
    wf_id = ex.json()["workflow_id"]
    for _ in range(60):
        time.sleep(2)
        rr = c.get(f"{BASE}/workflows/{wf_id}", headers=headers)
        if rr.status_code == 200 and rr.json().get("status") in ("completed", "failed"):
            break
    result = c.get(f"{BASE}/workflows/{wf_id}/result", headers=headers).json()
    tasks = c.get(f"{BASE}/workflows/{wf_id}/tasks", headers=headers).json()
    return status if (status := rr.json().get("status", "")) else "unknown", result, tasks


def main() -> int:
    c = httpx.Client(timeout=120)
    run = uuid.uuid4().hex[:8]
    a = register_login(c, f"rag-a2-{run}@example.com")
    b = register_login(c, f"rag-b2-{run}@example.com")

    up = c.post(
        f"{BASE}/documents",
        files={"file": ("vault-a.txt", f"Project vault access code is {SECRET}. Extremely confidential payroll detail.".encode(), "text/plain")},
        headers=a,
    )
    print("A upload:", up.status_code, up.json().get("document_id", up.text[:120]))

    # B queries the TOPIC only — never the secret phrase
    b_status, b_result, b_tasks = run_workflow(c, b, "Search enterprise knowledge for vault access codes and summarize any findings")
    blob_b = str(b_result) + str(b_tasks)
    leaked = SECRET in blob_b
    print("B workflow status:", b_status)
    print("B result/tasks contain SECRET:", leaked)
    if leaked:
        idx = blob_b.find(SECRET)
        print("LEAK CONTEXT:", blob_b[max(0, idx - 120): idx + 120])

    # A (owner) queries the topic — should retrieve their own document
    a_status, a_result, a_tasks = run_workflow(c, a, "Search enterprise knowledge for vault access codes and summarize any findings")
    blob_a = str(a_result) + str(a_tasks)
    owner_ok = SECRET in blob_a
    print("A workflow status:", a_status)
    print("A (owner) retrieves own secret:", owner_ok)

    ok = (not leaked)
    print("\nRESULT:", "ISOLATED (PASS)" if ok else "LEAK DETECTED (FAIL)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
