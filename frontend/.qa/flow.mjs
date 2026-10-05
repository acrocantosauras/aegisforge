// Phase 2.5 live E2E through the real frontend proxy + headless Chrome.
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const log = (...a) => console.log(...a);

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  await cdp.setViewport(1440, 900);
  const result = { steps: [], consoleErrors: [], exceptions: [], failedResponses: [] };

  try {
    // --- 1. Login page load (anonymous) ---
    await cdp.navigate(`${ORIGIN}/login`, 3000);
    result.steps.push({ step: "load /login", url: await cdp.evaluate("location.href"), title: await cdp.evaluate("document.title") });

    // --- 2. Register + login through the browser (Next proxy -> FastAPI) ---
    const email = `qa25-${Date.now()}@example.com`;
    const password = "Qa25Passw0rd!x";
    const authInfo = await cdp.evaluate(`(async () => {
      const email = ${JSON.stringify(email)};
      const password = ${JSON.stringify(password)};
      const reg = await fetch('/api/v1/auth/register', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ email, password, full_name: 'QA Phase25' }),
      });
      const regBody = await reg.json();
      const login = await fetch('/api/v1/auth/login', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ email, password }),
      });
      const loginBody = await login.json();
      if (loginBody.access_token) {
        localStorage.setItem('aegisforge_token', loginBody.access_token);
        localStorage.setItem('aegisforge_email', email);
      }
      return { registerStatus: reg.status, loginStatus: login.status, token: loginBody.access_token || null, email };
    })()`);
    result.steps.push({
      step: "register+login via browser fetch",
      registerStatus: authInfo.registerStatus,
      loginStatus: authInfo.loginStatus,
      gotToken: Boolean(authInfo.token),
      tokenLooksJwt: typeof authInfo.token === "string" && authInfo.token.split(".").length === 3,
    });
    if (!authInfo.token) throw new Error("no token; aborting");
    const token = authInfo.token;

    // --- 3. Dashboard (real data) ---
    await cdp.resetLogs();
    await cdp.navigate(`${ORIGIN}/dashboard`, 4000);
    const dash = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return { hasEmail: t.includes(${JSON.stringify(email)}), text: t.slice(0, 900) };
    })()`);
    result.steps.push({ step: "dashboard", hasEmail: dash.hasEmail, text: dash.text });

    // --- 4. Create request + async execute through the browser ---
    const intent =
      "Summarize the internal security policy for new engineers and list the required onboarding controls.";
    const created = await cdp.evaluate(`(async () => {
      const token = localStorage.getItem('aegisforge_token');
      const r = await fetch('/api/v1/requests', {
        method:'POST', headers:{'Content-Type':'application/json', Authorization:'Bearer '+token},
        body: JSON.stringify({ intent: ${JSON.stringify(intent)}, context: { team: 'platform' } }),
      });
      const body = await r.json();
      return { status: r.status, body };
    })()`);
    result.steps.push({ step: "create request", status: created.status, requestId: created.body?.id, reqStatus: created.body?.status });
    const requestId = created.body?.id;

    const exec = await cdp.evaluate(`(async () => {
      const token = localStorage.getItem('aegisforge_token');
      const r = await fetch('/api/v1/execution/requests/${requestId}/execute-async', {
        method:'POST', headers:{ Authorization:'Bearer '+token },
      });
      return { status: r.status, body: await r.json() };
    })()`);
    result.steps.push({ step: "execute-async", status: exec.status, jobId: exec.body?.job_id, workflowId: exec.body?.workflow_id });
    const workflowId = exec.body?.workflow_id;

    // --- 5. Poll workflow/tasks until terminal (real worker processing) ---
    let terminal = null;
    for (let i = 0; i < 90; i++) {
      await sleep(2000);
      const snap = await cdp.evaluate(`(async () => {
        const token = localStorage.getItem('aegisforge_token');
        const h = { Authorization: 'Bearer '+token };
        const wf = await (await fetch('/api/v1/workflows/by-request/${requestId}', {headers:h})).json();
        const tasks = await (await fetch('/api/v1/workflows/${workflowId}/tasks', {headers:h})).json();
        const req = await (await fetch('/api/v1/requests/${requestId}', {headers:h})).json();
        return { wfStatus: wf.status, reqStatus: req.status, tasks: (tasks.tasks||[]).map(t=>({id:t.task_id,status:t.status,agent:t.agent_type,dur:t.duration_ms,tools:t.tools_used,ev:t.evidence_count})) };
      })()`);
      if (["completed", "failed", "cancelled"].includes(snap.wfStatus) || ["completed", "failed"].includes(snap.reqStatus)) {
        terminal = snap;
        break;
      }
    }
    result.steps.push({ step: "worker processing", terminal });

    // --- 6. Execution workspace renders real state ---
    await cdp.resetLogs();
    await cdp.navigate(`${ORIGIN}/requests/${requestId}`, 6000);
    // let polling settle
    await sleep(3000);
    const workspace = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return {
        hasRequestId: t.includes(${JSON.stringify(requestId)}),
        hasWorkflowId: t.includes(${JSON.stringify(workflowId)}),
        hasIntent: t.includes(${JSON.stringify(intent.slice(0, 30))}),
        taskNodes: document.querySelectorAll('[data-task-id]').length,
        svgEdges: document.querySelectorAll('svg path, svg line').length,
        timelineItems: document.querySelectorAll('.timeline-item, [class*="timeline"] li, [class*="timeline"] > *').length,
        textLen: t.length,
        excerpt: t.slice(0, 1800),
      };
    })()`);
    result.steps.push({ step: "workspace", ...workspace });

    // --- 7. Refresh / recovery ---
    await cdp.navigate(`${ORIGIN}/requests/${requestId}`, 5000);
    const afterRefresh = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return { hasRequestId: t.includes(${JSON.stringify(requestId)}), taskNodes: document.querySelectorAll('[data-task-id]').length };
    })()`);
    result.steps.push({ step: "refresh recovery", ...afterRefresh });

    // --- 8. History ---
    await cdp.resetLogs();
    await cdp.navigate(`${ORIGIN}/history`, 4000);
    const history = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      return { showsRequest: t.includes(${JSON.stringify(intent.slice(0, 25))}) || t.includes(${JSON.stringify(requestId)}), text: t.slice(0,900) };
    })()`);
    result.steps.push({ step: "history", ...history });

    await cdp.screenshot(new URL("./shot-workspace-1440.png", import.meta.url).pathname.replace(/^\//, "").replace(/:\//, "://"));
    result.consoleErrors = cdp.console.filter((c) => c.type === "error").map((c) => c.text);
    result.exceptions = cdp.exceptions.slice();
    result.failedResponses = cdp.responses.filter((r) => r.status >= 400 && !r.url.includes("favicon"));
    log(JSON.stringify(result, null, 2));
  } catch (e) {
    result.error = String(e);
    log(JSON.stringify(result, null, 2));
    process.exitCode = 1;
  } finally {
    proc.kill();
  }
}

main();
