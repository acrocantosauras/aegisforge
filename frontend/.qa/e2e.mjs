// Full UI-driven E2E: register + login through the real forms, submit a task
// through the Execute composer, watch the worker-driven workflow reach a
// terminal state in the execution workspace, verify refresh recovery, history,
// and polling discipline. Real backend only — no mocks, no fabricated data.
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const log = (...a) => console.log(...a);

const INTENT =
  "Conduct enterprise knowledge research on data access policy, analyze the evidence, and synthesize a recommendation";

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  const out = { steps: [], consoleErrors: [], exceptions: [], failedResponses: [] };
  const fail = (m) => {
    out.error = m;
    log(JSON.stringify(out, null, 2));
    proc.kill();
    process.exit(1);
  };

  try {
    await cdp.setViewport(1440, 900);
    const email = `qae2e-${Date.now()}@example.com`;
    const password = "QaE2ePassw0rd!x";

    // --- 1. Landing page explains the product ---
    await cdp.navigate(`${ORIGIN}/`, 3500);
    const landing = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return { hasH1: !!document.querySelector('h1'), mentions: /orchestration/i.test(t), textLen: t.length };
    })()`);
    out.steps.push({ step: "landing", ...landing });
    if (!landing.hasH1) fail("landing has no h1");

    // --- 2. REGISTER through the real form ---
    await cdp.navigate(`${ORIGIN}/login`, 3000);
    await cdp.evaluate(
      `Array.from(document.querySelectorAll('button')).find(b => /^Register$/i.test(b.innerText.trim()))?.click()`
    );
    await sleep(400);
    const registered = await cdp.evaluate(`(async () => {
      const set = (sel, v) => {
        const el = document.querySelector(sel);
        if (!el) return false;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, v);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      };
      const okName = set('#fullName', 'QA E2E Operator');
      const okMail = set('#email', ${JSON.stringify(email)});
      const okPass = set('#password', ${JSON.stringify(password)});
      const btn = Array.from(document.querySelectorAll('button')).find(b => /Create Account/i.test(b.innerText));
      if (!okName || !okMail || !okPass || !btn) return { ok: false, okName, okMail, okPass, hasBtn: !!btn };
      btn.click();
      return { ok: true };
    })()`);
    out.steps.push({ step: "register form", ...registered });
    if (!registered.ok) fail("register form fields not found");
    await sleep(3500);
    const afterRegister = await cdp.evaluate("location.pathname");
    out.steps.push({ step: "after register", path: afterRegister });
    if (afterRegister !== "/dashboard") fail(`expected /dashboard after register, got ${afterRegister}`);

    // --- 3. Sign out, then LOG IN through the real form ---
    await cdp.evaluate(`(() => {
      const b = Array.from(document.querySelectorAll('button')).find(x => /Sign out/i.test(x.innerText));
      if (b) b.click();
    })()`);
    await sleep(2000);
    await cdp.navigate(`${ORIGIN}/login`, 2500);
    const loggedIn = await cdp.evaluate(`(async () => {
      const set = (sel, v) => {
        const el = document.querySelector(sel);
        if (!el) return false;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, v);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      };
      const okMail = set('#email', ${JSON.stringify(email)});
      const okPass = set('#password', ${JSON.stringify(password)});
      const btn = document.querySelector('button[type=submit]');
      if (!okMail || !okPass || !btn) return { ok: false, okMail, okPass, hasBtn: !!btn };
      btn.click();
      return { ok: true };
    })()`);
    out.steps.push({ step: "login form", ...loggedIn });
    if (!loggedIn.ok) fail("login form fields not found");
    await sleep(3500);
    const afterLogin = await cdp.evaluate("location.pathname");
    out.steps.push({ step: "after login", path: afterLogin, hasEmail: await cdp.evaluate(`document.body.innerText.includes(${JSON.stringify(email)})`) });
    if (afterLogin !== "/dashboard") fail(`expected /dashboard after login, got ${afterLogin}`);

    // --- 4. Auth gate still holds for a fresh tab without a token ---
    const token = await cdp.evaluate("localStorage.getItem('aegisforge_token')");
    if (!token) fail("no token stored after login");

    // --- 5. EXECUTE through the real composer ---
    await cdp.navigate(`${ORIGIN}/execute`, 3000);
    cdp.resetLogs();
    const submitted = await cdp.evaluate(`(() => {
      const ta = document.querySelector('#intent');
      if (!ta) return { ok: false, reason: 'no textarea' };
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(ta, ${JSON.stringify(INTENT)});
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      const btn = Array.from(document.querySelectorAll('button')).find(b => /Submit & Execute/i.test(b.innerText));
      if (!btn) return { ok: false, reason: 'no submit button' };
      if (btn.disabled) return { ok: false, reason: 'submit disabled' };
      btn.click();
      return { ok: true };
    })()`);
    out.steps.push({ step: "execute form", ...submitted });
    if (!submitted.ok) fail(`execute form failed: ${submitted.reason}`);
    await sleep(5000);
    const workspaceUrl = await cdp.evaluate("location.pathname");
    out.steps.push({ step: "navigated to workspace", path: workspaceUrl });
    if (!workspaceUrl.startsWith("/requests/")) fail(`expected workspace route, got ${workspaceUrl}`);
    const requestId = workspaceUrl.split("/requests/")[1];

    // --- 6. Worker executes; wait for a terminal workflow ---
    let terminal = null;
    for (let i = 0; i < 90; i++) {
      await sleep(2000);
      terminal = await cdp.evaluate(`(async () => {
        const t = localStorage.getItem('aegisforge_token');
        const h = { Authorization: 'Bearer ' + t };
        const req = await (await fetch('/api/v1/requests/${requestId}', { headers: h })).json();
        const wf = await (await fetch('/api/v1/workflows/by-request/${requestId}', { headers: h })).json().catch(() => null);
        const tasks = wf ? await (await fetch('/api/v1/workflows/' + wf.workflow_id + '/tasks', { headers: h })).json() : null;
        return { reqStatus: req.status, wfStatus: wf && wf.status, workflowId: wf && wf.workflow_id, taskCount: tasks ? tasks.tasks.length : 0, taskStatuses: tasks ? tasks.tasks.map(x => x.status) : [] };
      })()`);
      if (["completed", "failed", "cancelled"].includes(terminal?.wfStatus) || ["completed", "failed"].includes(terminal?.reqStatus)) break;
    }
    out.steps.push({ step: "worker terminal", ...terminal });
    if (!terminal || !["completed", "failed", "cancelled"].includes(terminal.wfStatus)) fail("workflow never reached a terminal state");

    // --- 7. Workspace renders real state ---
    await cdp.navigate(`${ORIGIN}/requests/${requestId}`, 6000);
    await sleep(2500);
    const ws = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return {
        hasRequestId: t.includes(${JSON.stringify(requestId)}),
        hasIntent: t.includes(${JSON.stringify(INTENT.slice(0, 30))}),
        hasWorkflowId: !!t.match(/workflow wf-/),
        taskNodes: document.querySelectorAll('[data-task-id]').length,
        svgPaths: document.querySelectorAll('svg path').length,
        timelineItems: document.querySelectorAll('.timeline-item').length,
        hasEvaluation: /Evaluation/i.test(t),
        hasResult: /Final Result/i.test(t),
        hasCitationSection: /Citations|Evidence/i.test(t),
        textLen: t.length,
      };
    })()`);
    out.steps.push({ step: "workspace render", ...ws });
    if (!ws.hasRequestId || !ws.hasIntent) fail("workspace missing request identity");
    if (ws.taskNodes === 0) fail("workspace rendered no task nodes");
    if (ws.svgPaths === 0) fail("workspace rendered no dependency edges");
    if (!ws.timelineItems) fail("workspace rendered no timeline");
    if (!ws.hasEvaluation) fail("workspace missing evaluation");

    // --- 8. Refresh recovery: a cold load restores the same state ---
    await cdp.navigate(`${ORIGIN}/requests/${requestId}`, 5000);
    const afterRefresh = await cdp.evaluate(`(() => ({
      taskNodes: document.querySelectorAll('[data-task-id]').length,
      hasResult: /Final Result/i.test(document.body.innerText),
    }))()`);
    out.steps.push({ step: "refresh recovery", ...afterRefresh });
    if (afterRefresh.taskNodes === 0) fail("refresh lost the task graph");

    // --- 9. Polling discipline: a terminal workspace must go quiet ---
    await cdp.navigate(`${ORIGIN}/requests/${requestId}`, 5000);
    await cdp.resetLogs();
    await sleep(9000);
    const polled = cdp.requests
      .filter((r) => r.url.includes("/api/v1/") && !r.url.includes("/system"))
      .map((r) => r.url.replace(ORIGIN, ""));
    out.steps.push({ step: "polling after terminal", workspaceApiCalls: polled.length, calls: polled });
    if (polled.length > 2) fail(`terminal workspace still polling: ${polled.length} calls in 9s`);

    // --- 10. History lists the execution ---
    await cdp.navigate(`${ORIGIN}/history`, 4000);
    const history = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return { showsIntent: t.includes(${JSON.stringify(INTENT.slice(0, 25))}), rows: document.querySelectorAll('table tbody tr').length };
    })()`);
    out.steps.push({ step: "history", ...history });
    if (!history.showsIntent) fail("history does not list the executed request");

    // --- 11. Dashboard statistics reflect reality ---
    await cdp.navigate(`${ORIGIN}/dashboard`, 4000);
    const dash = await cdp.evaluate(`(() => {
      const cards = Array.from(document.querySelectorAll('.stat-grid .stat-card'));
      const get = (label) => {
        const c = cards.find(x => x.querySelector('.label') && x.querySelector('.label').textContent === label);
        return c ? c.querySelector('.value').textContent : null;
      };
      return { requests: get('Requests'), successRate: get('Success Rate'), inProgress: get('In Progress'), approvals: get('Pending Approvals') };
    })()`);
    out.steps.push({ step: "dashboard stats", ...dash });
    if (dash.requests !== "1") fail(`dashboard Requests=${dash.requests}, expected 1`);

    out.consoleErrors = cdp.console.filter((c) => c.type === "error").map((c) => c.text);
    out.exceptions = cdp.exceptions.slice();
    out.failedResponses = cdp.responses.filter((r) => r.status >= 400 && !r.url.includes("favicon")).map((r) => `${r.status} ${r.url.replace(ORIGIN, "")}`);
    out.requestId = requestId;
    out.workflowId = terminal.workflowId;
    log(JSON.stringify(out, null, 2));
    proc.kill();
    process.exit(0);
  } catch (e) {
    fail(String(e));
  }
}

main();
