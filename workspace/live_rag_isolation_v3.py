"""LIVE RAG ISOLATION — FINAL PROOF (v3).

Exercises the true agent-mediated RAG path through the real Docker stack:
  register -> login -> upload -> async execute -> Redis -> worker ->
  planner (RAG task, threshold 0.0) -> RAGAgent -> pgvector retrieval.

User A ingests a secret document. User B triggers a RAG workflow on the
TOPIC (never the secret). With the P0, B retrieved A's chunk. Post-fix,
B must receive zero chunks/citations; A must still retrieve their own.
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
    status = ""
    for _ in range(60):
        time.sleep(2)
        rr = c.get(f"{BASE}/workflows/{wf_id}", headers=headers)
        if rr.status_code == 200:
            status = rr.json().get("status", "")
            if status in ("completed", "failed"):
                break
    result = c.get(f"{BASE}/workflows/{wf_id}/result", headers=headers).json()
    tasks = c.get(f"{BASE}/workflows/{wf_id}/tasks", headers=headers).json()
    return status, result, tasks


def main() -> int:
    c = httpx.Client(timeout=120)
    run = uuid.uuid4().hex[:8]
    a = register_login(c, f"ragf-a-{run}@example.com")
    b = register_login(c, f"ragf-b-{run}@example.com")

    up = c.post(
        f"{BASE}/documents",
        files={"file": ("vault-a.txt", f"Project vault access code is {SECRET}. Extremely confidential payroll detail.".encode(), "text/plain")},
        headers=a,
    )
    print("A upload:", up.status_code, up.json().get("document_id", up.text[:120]))

    # B: RAG-triggering intent, topic words only (threshold 0.0 in planner)
    intent_b = "Conduct enterprise knowledge research on vault access codes and payroll, analyze the evidence, and synthesize a recommendation"
    b_status, b_result, b_tasks = run_workflow(c, b, intent_b)
    blob_b = str(b_result) + str(b_tasks)
    leaked = SECRET in blob_b
    print("B workflow status:", b_status)
    print("B agents used:", sorted({t.get("agent_type") for t in b_tasks.get("tasks", [])}))
    print("B result/tasks contain SECRET:", leaked)
    if leaked:
        idx = blob_b.find(SECRET)
        print("LEAK CONTEXT:", blob_b[max(0, idx - 150): idx + 150])

    # A (owner): same RAG path — must retrieve own secret
    a_status, a_result, a_tasks = run_workflow(c, a, intent_b)
    blob_a = str(a_result) + str(a_tasks)
    owner_ok = SECRET in blob_a
    print("A workflow status:", a_status)
    print("A agents used:", sorted({t.get("agent_type") for t in a_tasks.get("tasks", [])}))
    print("A (owner) retrieves own secret:", owner_ok)

    ok = (not leaked) and owner_ok
    print("\nRESULT:", "ISOLATED + OWNER OK (PASS)" if ok else ("LEAK (FAIL)" if leaked else "OWNER BLOCKED (investigate retrieval wiring)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
