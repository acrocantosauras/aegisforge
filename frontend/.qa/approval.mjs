// Approval flow QA: the paused workflow must surface in the workspace and the
// approval queue, be approvable through the real UI form, and resume to a
// terminal state. Requires an env-provided request id of a workflow that is
// currently waiting for approval.
//   REQUEST_ID=... ACCOUNT_EMAIL=... node .qa/approval.mjs
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const log = (...a) => console.log(...a);
const REQUEST_ID = process.env.REQUEST_ID || "";
const ACCOUNT_EMAIL = process.env.ACCOUNT_EMAIL || "";

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  const out = { steps: [], consoleErrors: [], exceptions: [] };

  try {
    await cdp.setViewport(1440, 900);
    await cdp.navigate(`${ORIGIN}/login`, 2500);
    // Re-authenticate the account that owns the paused request. The persisted
    // Chrome profile can hold an expired token, so this login is mandatory.
    const auth = await cdp.evaluate(`(async () => {
      const r = await fetch('/api/v1/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:${JSON.stringify(ACCOUNT_EMAIL)},password:'ProbePass123!'})});
      const b = await r.json().catch(() => ({}));
      if (b.access_token) {
        localStorage.setItem('aegisforge_token', b.access_token);
        localStorage.setItem('aegisforge_email', ${JSON.stringify(ACCOUNT_EMAIL)});
      }
      return { status: r.status, got: !!b.access_token, detail: b.detail || null };
    })()`);
    out.steps.push({ step: "re-authenticate", ...auth });
    if (!auth.got) throw new Error(`login failed: ${JSON.stringify(auth)}`);

    // --- 1. Workspace surfaces the approval gate ---
    await cdp.navigate(`${ORIGIN}/requests/${REQUEST_ID}`, 5500);
    await sleep(2000);
    const ws = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return {
        hasApprovalBanner: /HUMAN APPROVAL REQUIRED/.test(t),
        hasWaitingBadge: /Needs Approval|Approval Gate|action requires approval/i.test(t),
        approvalTaskNodes: Array.from(document.querySelectorAll('[data-task-id]')).filter(n => /approval/i.test(n.innerText)).length,
        taskNodes: document.querySelectorAll('[data-task-id]').length,
      };
    })()`);
    out.steps.push({ step: "workspace approval gate", ...ws });
    if (!ws.hasApprovalBanner || ws.taskNodes === 0) {
      // Diagnose before failing: is it auth, the request lookup, or rendering?
      const diag = await cdp.evaluate(`(async () => {
        const t = localStorage.getItem('aegisforge_token');
        const h = { Authorization: 'Bearer ' + t };
        const req = await fetch('/api/v1/requests/${REQUEST_ID}', { headers: h });
        const wf = await fetch('/api/v1/workflows/by-request/${REQUEST_ID}', { headers: h });
        return { path: location.pathname, reqStatus: req.status, wfStatus: wf.status, body: document.body.innerText.replace(/\\n+/g, ' | ').slice(0, 400) };
      })()`);
      out.steps.push({ step: "diagnostics", ...diag });
      throw new Error("workspace did not surface the approval gate");
    }

    // --- 2. Approval queue lists it ---
    await cdp.navigate(`${ORIGIN}/approvals`, 4500);
    const queue = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return {
        hasPending: /pending/i.test(t),
        hasRisk: /high risk|critical risk/i.test(t),
        hasApproveBtn: !!Array.from(document.querySelectorAll('button')).find(b => /^Approve$/i.test(b.innerText.trim())),
        hasRejectBtn: !!Array.from(document.querySelectorAll('button')).find(b => /^Reject$/i.test(b.innerText.trim())),
        hasReasonInput: !!document.querySelector('input[id^="reason-"]'),
      };
    })()`);
    out.steps.push({ step: "approval queue", ...queue });
    if (!queue.hasApproveBtn || !queue.hasRejectBtn) throw new Error("approve/reject controls missing");

    // --- 3. Approve through the real form ---
    cdp.resetLogs();
    const approved = await cdp.evaluate(`(() => {
      const input = document.querySelector('input[id^="reason-"]');
      if (input) {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, 'Verified in live QA: proceed with synthesis.');
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      const btn = Array.from(document.querySelectorAll('button')).find(b => /^Approve$/i.test(b.innerText.trim()));
      if (!btn) return { ok: false };
      btn.click();
      return { ok: true };
    })()`);
    out.steps.push({ step: "approve click", ...approved });
    if (!approved.ok) throw new Error("approve button not clickable");
    await sleep(3000);

    const afterApprove = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return { stillPending: /pending/i.test(t), recentDecisions: /Recent Decisions/i.test(t), decisionError: (document.querySelector('.error')||{}).innerText || '' };
    })()`);
    out.steps.push({ step: "after approve", ...afterApprove });

    // --- 4. Workflow resumes and reaches a terminal state ---
    let terminal = null;
    for (let i = 0; i < 60; i++) {
      await sleep(2000);
      terminal = await cdp.evaluate(`(async () => {
        const t = localStorage.getItem('aegisforge_token');
        const h = { Authorization: 'Bearer ' + t };
        const req = await (await fetch('/api/v1/requests/${REQUEST_ID}', { headers: h })).json();
        const wf = await (await fetch('/api/v1/workflows/by-request/${REQUEST_ID}', { headers: h })).json().catch(() => null);
        const tasks = wf ? await (await fetch('/api/v1/workflows/' + wf.workflow_id + '/tasks', { headers: h })).json() : null;
        return { reqStatus: req.status, wfStatus: wf && wf.status, taskStatuses: tasks ? tasks.tasks.map(x => x.status) : [] };
      })()`);
      if (["completed", "failed", "cancelled"].includes(terminal?.wfStatus)) break;
    }
    out.steps.push({ step: "resumed workflow", ...terminal });
    if (!["completed", "failed", "cancelled"].includes(terminal?.wfStatus)) throw new Error("workflow did not resume after approval");

    // --- 5. Workspace reflects the decided state ---
    await cdp.navigate(`${ORIGIN}/requests/${REQUEST_ID}`, 6000);
    await sleep(2500);
    const finalWs = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return {
        approvalBannerGone: !/HUMAN APPROVAL REQUIRED/.test(t),
        hasResult: /Final Result/i.test(t),
        taskNodes: document.querySelectorAll('[data-task-id]').length,
        textLen: t.length,
      };
    })()`);
    out.steps.push({ step: "workspace after approval", ...finalWs });

    out.consoleErrors = cdp.console.filter((c) => c.type === "error").map((c) => c.text);
    out.exceptions = cdp.exceptions.slice();
    log(JSON.stringify(out, null, 2));
    proc.kill();
    process.exit(0);
  } catch (e) {
    out.error = String(e);
    log(JSON.stringify(out, null, 2));
    proc.kill();
    process.exit(1);
  }
}

main();
