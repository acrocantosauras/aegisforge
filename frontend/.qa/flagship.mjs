// Flagship demo end-to-end (Phase 10) — real browser, real backend, real RAG.
//
//   register (UI form) → seed the demo knowledge base through the real
//   ingestion pipeline → verify it in the Knowledge page → run the canonical
//   request from the Execute composer → worker execution → execution workspace
//   (graph, timeline, evaluation, grounded result with evidence) → refresh
//   recovery → polling discipline → history → dashboard → owner isolation →
//   repeated execution.
//
// Nothing is mocked: the demo corpus is seeded with scripts/seed_demo.py and
// every assertion is made against what the platform actually returns.
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const PYTHON = process.env.QA_PYTHON || path.join(REPO, ".venv", "Scripts", "python.exe");
const SEEDER = path.join(REPO, "scripts", "seed_demo.py");

const INTENT =
  "Conduct enterprise knowledge research on the Acme Systems data platform procurement decision: compare Northwind Streamline and Helios Fabric against our internal architecture, security, data governance, infrastructure and cost requirements, identify the conflicts between vendor claims and internal policy, and synthesize a recommendation.";

const log = (...a) => console.log(...a);

function seed(email, extraArgs = []) {
  try {
    return execFileSync(PYTHON, [SEEDER, "--email", email, ...extraArgs], {
      cwd: REPO,
      encoding: "utf8",
      env: {
        ...process.env,
        DATABASE_URL:
          process.env.QA_DATABASE_URL ||
          "postgresql+psycopg://aegisforge:aegisforge@localhost:5432/aegisforge",
        ENVIRONMENT: "development",
        SECRET_KEY: process.env.SECRET_KEY || "flagship-demo-local-secret",
      },
    });
  } catch (err) {
    throw new Error(`seed failed: ${err.stdout || ""}${err.stderr || ""}`);
  }
}

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  const out = { steps: [] };
  const fail = (m) => {
    out.error = m;
    log(JSON.stringify(out, null, 2));
    proc.kill();
    process.exit(1);
  };

  try {
    await cdp.setViewport(1440, 900);
    const stamp = Date.now();
    const email = `flagship-${stamp}@example.com`;
    const password = "FlagshipDemo1!x";

    // --- 1. Register through the real form ---
    await cdp.navigate(`${ORIGIN}/login`, 3000);
    await cdp.evaluate(
      `Array.from(document.querySelectorAll('button')).find(b => /^Register$/i.test(b.innerText.trim()))?.click()`
    );
    await sleep(500);
    const registered = await cdp.evaluate(`(() => {
      const set = (sel, v) => {
        const el = document.querySelector(sel);
        if (!el) return false;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, v);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      };
      const ok = set('#fullName', 'Flagship Operator')
        && set('#email', ${JSON.stringify(email)})
        && set('#password', ${JSON.stringify(password)});
      const btn = Array.from(document.querySelectorAll('button')).find(b => /Create Account/i.test(b.innerText));
      if (!ok || !btn) return { ok: false, ok, hasBtn: !!btn };
      btn.click();
      return { ok: true };
    })()`);
    if (!registered.ok) fail("register form fields not found");
    await sleep(3500);
    const afterRegister = await cdp.evaluate("location.pathname");
    out.steps.push({ step: "register", path: afterRegister, email });
    if (afterRegister !== "/dashboard") fail(`expected /dashboard after register, got ${afterRegister}`);

    // --- 2. Seed the demo corpus through the real ingestion pipeline ---
    const seedOut = seed(email);
    out.steps.push({ step: "seed", summary: seedOut.trim().split("\n").slice(-2).join(" | ") });
    const reseedOut = seed(email); // idempotency, live
    out.steps.push({ step: "reseed", summary: reseedOut.trim().split("\n").slice(-2).join(" | ") });
    if (!/9 unchanged/.test(reseedOut)) fail("second seed was not a no-op:\n" + reseedOut);

    // --- 3. The knowledge base is really there (UI) ---
    await cdp.navigate(`${ORIGIN}/documents`, 4000);
    const docs = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return {
        mentionsAcme: t.includes('Acme Systems'),
        mentionsNorthwind: t.includes('Northwind Streamline'),
        mentionsHelios: t.includes('Helios Fabric'),
        chunkNumbers: (t.match(/\\d+ chunks?/g) || []).length,
      };
    })()`);
    out.steps.push({ step: "knowledge page", ...docs });
    if (!docs.mentionsAcme || !docs.mentionsNorthwind) fail("seeded demo documents are not listed");

    // --- 4. Submit the flagship request from the flagship example ---
    await cdp.navigate(`${ORIGIN}/execute`, 4000);
    const picked = await cdp.evaluate(`(() => {
      const btn = Array.from(document.querySelectorAll('.intent-example'))
        .find(b => /Flagship demo/i.test(b.innerText));
      if (!btn) return { ok: false };
      btn.click();
      return { ok: true, value: document.querySelector('#intent')?.value || '' };
    })()`);
    if (!picked.ok) fail("flagship example button not found");
    await sleep(500);
    const composerHasIntent = await cdp.evaluate(
      `document.querySelector('#intent')?.value?.startsWith('Conduct enterprise knowledge research on the Acme') === true`
    );
    out.steps.push({ step: "flagship example", composerHasIntent });
    if (!composerHasIntent) fail("flagship example did not populate the composer");

    cdp.resetLogs();
    const submitted = await cdp.evaluate(`(() => {
      const btn = Array.from(document.querySelectorAll('button')).find(b => /Submit & Execute/i.test(b.innerText));
      if (!btn || btn.disabled) return { ok: false };
      btn.click();
      return { ok: true };
    })()`);
    if (!submitted.ok) fail("submit button unavailable");
    await sleep(5000);
    const workspacePath = await cdp.evaluate("location.pathname");
    if (!workspacePath.startsWith("/requests/")) fail(`expected workspace, got ${workspacePath}`);
    const requestId = workspacePath.split("/requests/")[1];
    out.steps.push({ step: "submitted", requestId });

    // --- 5. Distributed worker drives it to a terminal state ---
    let terminal = null;
    for (let i = 0; i < 90; i++) {
      await sleep(2000);
      terminal = await cdp.evaluate(`(async () => {
        const t = localStorage.getItem('aegisforge_token');
        const h = { Authorization: 'Bearer ' + t };
        const req = await (await fetch('/api/v1/requests/${requestId}', { headers: h })).json();
        const wf = await (await fetch('/api/v1/workflows/by-request/${requestId}', { headers: h })).json().catch(() => null);
        const tasks = wf ? await (await fetch('/api/v1/workflows/' + wf.workflow_id + '/tasks', { headers: h })).json() : null;
        return {
          reqStatus: req.status,
          wfStatus: wf && wf.status,
          workflowId: wf && wf.workflow_id,
          agents: tasks ? tasks.tasks.map(x => x.agent_type) : [],
          taskStatuses: tasks ? tasks.tasks.map(x => x.status) : [],
        };
      })()`);
      if (["completed", "failed", "cancelled"].includes(terminal?.wfStatus)) break;
    }
    out.steps.push({ step: "worker terminal", ...terminal });
    if (terminal?.wfStatus !== "completed") fail(`workflow did not complete: ${JSON.stringify(terminal)}`);
    const agentSet = [...new Set(terminal.agents)].sort();
    if (JSON.stringify(agentSet) !== JSON.stringify(["analysis", "rag", "research", "synthesis"])) {
      fail(`unexpected agent graph: ${JSON.stringify(agentSet)}`);
    }
    if (terminal.taskStatuses.some((s) => s !== "completed")) fail("not every task completed");

    // --- 6. Workspace shows the whole pipeline ---
    await cdp.navigate(`${ORIGIN}/requests/${requestId}`, 6000);
    await sleep(2000);
    await cdp.screenshot(path.join(HERE, "flagship-workspace.png"));
    const ws = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return {
        taskNodes: document.querySelectorAll('[data-task-id]').length,
        agentsShown: Array.from(document.querySelectorAll('.task-node-agent'))
          .map(n => n.textContent.trim().split(/\\s+/).pop()),
        edges: document.querySelectorAll('svg path').length,
        waveLabels: Array.from(document.querySelectorAll('.task-wave-label')).map(n => n.textContent.trim()),
        timelineItems: document.querySelectorAll('.timeline-item').length,
        hasEvaluation: /Evaluation/i.test(t),
        evalScores: Array.from(document.querySelectorAll('.eval-score .v')).map(n => n.textContent.trim()),
        hasResult: /Final Result/i.test(t),
        hasKeyFindings: /Key findings/i.test(t),
        hasConflicts: /Conflicts detected/i.test(t),
        hasSources: /Sources/i.test(t),
        citationRows: document.querySelectorAll('.citation-row').length,
        evidenceSources: (t.match(/Evidence · (\\d+) retrieved source/) || [])[1] || null,
        toolChips: Array.from(document.querySelectorAll('.evidence-chips .tool-chip')).map(n => n.textContent.trim()),
        resultLen: (document.querySelector('.result-answer')?.innerText || '').length,
      };
    })()`);
    out.steps.push({ step: "workspace", ...ws });
    if (ws.taskNodes !== 4) fail(`expected 4 task nodes, got ${ws.taskNodes}`);
    if (ws.edges < 3) fail(`expected dependency edges, got ${ws.edges}`);
    if (!ws.timelineItems) fail("no execution timeline");
    if (!ws.hasEvaluation) fail("no evaluation panel");
    if (!ws.hasResult) fail("no final result panel");
    if (!ws.hasKeyFindings) fail("result has no sourced findings section");
    if (!ws.hasConflicts) fail("result does not surface detected conflicts");
    if (!ws.hasSources) fail("result has no sources section");
    if (ws.citationRows < 4) fail(`expected real citations, got ${ws.citationRows}`);
    if (!ws.toolChips.includes("knowledge.search")) fail("tool usage not shown");
    if (ws.resultLen < 400) fail(`final result suspiciously short (${ws.resultLen} chars)`);

    // --- 7. Task detail exposes real tool + evidence metadata ---
    await cdp.evaluate(`document.querySelector('[data-task-id]')?.click()`);
    await sleep(1200);
    const detail = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return {
        hasDetail: /Execution Metadata/i.test(t),
        mentionsTool: /knowledge\\.search/.test(t),
        mentionsEvidence: /Evidence/i.test(t),
      };
    })()`);
    out.steps.push({ step: "task detail", ...detail });
    if (!detail.hasDetail || !detail.mentionsTool) fail("task detail panel missing tool metadata");

    // --- 8. Refresh recovery + polling discipline ---
    await cdp.navigate(`${ORIGIN}/requests/${requestId}`, 5000);
    const afterRefresh = await cdp.evaluate(`(() => ({
      taskNodes: document.querySelectorAll('[data-task-id]').length,
      hasResult: /Final Result/i.test(document.body.innerText),
      citations: document.querySelectorAll('.citation-row').length,
    }))()`);
    out.steps.push({ step: "refresh recovery", ...afterRefresh });
    if (afterRefresh.taskNodes !== 4 || !afterRefresh.hasResult || afterRefresh.citations < 4) {
      fail("refresh did not restore the full workspace");
    }
    await cdp.resetLogs();
    await sleep(9000);
    const polled = cdp.requests
      .filter((r) => r.url.includes("/api/v1/") && !r.url.includes("/system"))
      .map((r) => r.url.replace(ORIGIN, ""));
    out.steps.push({ step: "polling after terminal", apiCalls: polled.length });
    if (polled.length > 2) fail(`terminal workspace still polling (${polled.length} calls)`);

    // --- 9. History + dashboard reflect the run ---
    await cdp.navigate(`${ORIGIN}/history`, 4000);
    const history = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      const links = Array.from(document.querySelectorAll('a[href^="/requests/"]')).map(a => a.getAttribute('href'));
      return {
        showsIntent: t.includes('Acme Systems data platform procurement'),
        rows: document.querySelectorAll('table tbody tr').length,
        linksToRun: links.includes('/requests/${requestId}'),
      };
    })()`);
    out.steps.push({ step: "history", ...history });
    if (!history.showsIntent || !history.linksToRun) fail("history does not list the flagship run");

    await cdp.navigate(`${ORIGIN}/dashboard`, 4000);
    const dash = await cdp.evaluate(`(() => {
      const cards = Array.from(document.querySelectorAll('.stat-grid .stat-card'));
      const get = (label) => {
        const c = cards.find(x => x.querySelector('.label') && x.querySelector('.label').textContent === label);
        return c ? c.querySelector('.value').textContent : null;
      };
      return { requests: get('Requests'), successRate: get('Success Rate'), inProgress: get('In Progress') };
    })()`);
    out.steps.push({ step: "dashboard", ...dash });
    if (dash.requests !== "1" || dash.successRate !== "100%") {
      fail(`dashboard stats wrong: ${JSON.stringify(dash)}`);
    }

    // --- 10. Owner isolation in the UI: another account cannot see the run ---
    const intruderEmail = `intruder-${stamp}@example.com`;
    const intruder = await cdp.evaluate(`(async () => {
      const reg = await fetch('/api/v1/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: ${JSON.stringify(intruderEmail)}, password: 'IntruderPass1!x', full_name: 'Other User' }),
      });
      const login = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: ${JSON.stringify(intruderEmail)}, password: 'IntruderPass1!x' }),
      });
      const body = await login.json();
      const probe = await fetch('/api/v1/workflows/${requestId}/result', { headers: { Authorization: 'Bearer ' + body.access_token } });
      const byReq = await fetch('/api/v1/workflows/by-request/${requestId}', { headers: { Authorization: 'Bearer ' + body.access_token } });
      return { registered: reg.status, probeStatus: probe.status, byRequestStatus: byReq.status };
    })()`);
    out.steps.push({ step: "owner isolation", ...intruder });
    if (intruder.probeStatus !== 404 || intruder.byRequestStatus !== 404) {
      fail(`demo run visible to another account: ${JSON.stringify(intruder)}`);
    }

    // --- 11. Repeated execution must not corrupt state ---
    await cdp.navigate(`${ORIGIN}/execute`, 4000);
    await cdp.evaluate(`(() => {
      const btn = Array.from(document.querySelectorAll('.intent-example')).find(b => /Flagship demo/i.test(b.innerText));
      btn?.click();
    })()`);
    await sleep(400);
    await cdp.evaluate(`(() => {
      const btn = Array.from(document.querySelectorAll('button')).find(b => /Submit & Execute/i.test(b.innerText));
      btn?.click();
    })()`);
    await sleep(5000);
    const secondPath = await cdp.evaluate("location.pathname");
    if (!secondPath.startsWith("/requests/")) fail(`second run did not open a workspace: ${secondPath}`);
    const secondId = secondPath.split("/requests/")[1];
    let secondTerminal = null;
    for (let i = 0; i < 90; i++) {
      await sleep(2000);
      secondTerminal = await cdp.evaluate(`(async () => {
        const t = localStorage.getItem('aegisforge_token');
        const h = { Authorization: 'Bearer ' + t };
        const req = await (await fetch('/api/v1/requests/${secondId}', { headers: h })).json();
        const wf = await (await fetch('/api/v1/workflows/by-request/${secondId}', { headers: h })).json().catch(() => null);
        return { reqStatus: req.status, wfStatus: wf && wf.status, workflowId: wf && wf.workflow_id };
      })()`);
      if (["completed", "failed", "cancelled"].includes(secondTerminal?.wfStatus)) break;
    }
    out.steps.push({ step: "repeated run", requestId: secondId, ...secondTerminal });
    if (secondTerminal?.wfStatus !== "completed") fail("repeated demo run did not complete");
    if (secondId === requestId) fail("repeated run reused the same request");

    await cdp.navigate(`${ORIGIN}/requests/${secondId}`, 6000);
    await sleep(1500);
    const secondWs = await cdp.evaluate(`(() => ({
      taskNodes: document.querySelectorAll('[data-task-id]').length,
      citations: document.querySelectorAll('.citation-row').length,
      hasConflicts: /Conflicts detected/i.test(document.body.innerText),
    }))()`);
    out.steps.push({ step: "repeated run workspace", ...secondWs });
    if (secondWs.taskNodes !== 4 || secondWs.citations < 4) fail("repeated run produced a degraded workspace");

    // --- 12. Clean console ---
    out.consoleErrors = cdp.console.filter((c) => c.type === "error").map((c) => c.text);
    out.exceptions = cdp.exceptions.slice();
    out.failedResponses = cdp.responses
      .filter((r) => r.status >= 400 && !r.url.includes("favicon"))
      .map((r) => `${r.status} ${r.url.replace(ORIGIN, "")}`);
    out.demoUser = email;
    log(JSON.stringify(out, null, 2));
    proc.kill();
    process.exit(0);
  } catch (e) {
    fail(String(e && e.stack ? e.stack : e));
  }
}

main();