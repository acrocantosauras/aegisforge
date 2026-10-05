// Phase 2.5 edge-case QA: API-unavailable error states + polling discipline.
// Real headless Chrome, no mocks beyond CDP network blocking (a real
// transport-level failure).
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const log = (...a) => console.log(...a);

async function seedToken(cdp, email, password) {
  return cdp.evaluate(`(async () => {
    await fetch('/api/v1/auth/register', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:${JSON.stringify(
      email
    )},password:${JSON.stringify(password)},full_name:'QA Edge'})}).catch(()=>{});
    const r = await fetch('/api/v1/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:${JSON.stringify(
      email
    )},password:${JSON.stringify(password)}})});
    const b = await r.json();
    if (b.access_token) {
      localStorage.setItem('aegisforge_token', b.access_token);
      localStorage.setItem('aegisforge_email', ${JSON.stringify(email)});
    }
    return { status: r.status, token: b.access_token || null };
  })()`);
}

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  const out = { errorStates: [], polling: null, consoleErrors: [], exceptions: [] };

  try {
    await cdp.setViewport(1440, 900);
    await cdp.navigate(`${ORIGIN}/login`, 3000);

    // Seed a real token.
    const email = `qa25edge-${Date.now()}@example.com`;
    const auth = await seedToken(cdp, email, "Qa25Passw0rd!x");
    out.auth = { loginStatus: auth.status, gotToken: Boolean(auth.token) };

    // Create + execute a real request so we have a terminal workflow to watch.
    const intent = "Research the onboarding security controls and cite the policy.";
    const created = await cdp.evaluate(`(async () => {
      const t = localStorage.getItem('aegisforge_token');
      const h = {'Content-Type':'application/json', Authorization:'Bearer '+t};
      const r = await fetch('/api/v1/requests', {method:'POST',headers:h,body:JSON.stringify({intent:${JSON.stringify(
        intent
      )},context:{}})});
      const b = await r.json();
      const e = await fetch('/api/v1/execution/requests/'+b.id+'/execute-async',{method:'POST',headers:{Authorization:'Bearer '+t}});
      return { requestId: b.id, execStatus: e.status };
    })()`);
    out.request = created;

    // Wait for terminal state.
    let wfStatus = "";
    for (let i = 0; i < 40; i++) {
      await sleep(2000);
      wfStatus = await cdp.evaluate(`(async () => {
        const t = localStorage.getItem('aegisforge_token');
        const r = await fetch('/api/v1/workflows/by-request/${created.requestId}',{headers:{Authorization:'Bearer '+t}});
        if (!r.ok) return 'err';
        return (await r.json()).status;
      })()`);
      if (["completed", "failed", "cancelled"].includes(wfStatus)) break;
    }
    out.terminalStatus = wfStatus;

    // --- Polling discipline: on a terminal workspace, count API calls over 8s.
    await cdp.resetLogs();
    await cdp.navigate(`${ORIGIN}/requests/${created.requestId}`, 5000);
    await cdp.resetLogs();
    await sleep(8000);
    out.polling = {
      apiCalls: cdp.requests
        .filter((r) => r.url.includes("/api/v1/"))
        .map((r) => r.url.replace(ORIGIN, "")),
      total: cdp.requests.filter((r) => r.url.includes("/api/v1/")).length,
    };

    // --- Error state: block all API traffic and load each page.
    await cdp.send("Network.setBlockedURLs", { urls: [`${ORIGIN}/api/*`, "http://localhost:8000/api/*"] });
    for (const route of ["/dashboard", "/history", "/system", "/agents", "/tools"]) {
      cdp.resetLogs();
      await cdp.navigate(`${ORIGIN}${route}`, 4500);
      const info = await cdp.evaluate(`(() => {
        const t = (document.body.innerText || '');
        return {
          path: location.pathname,
          textLen: t.length,
          blank: t.trim().length < 40,
          hasErrorRole: !!document.querySelector('[role=alert]'),
          hasRetry: /retry|try again|reload/i.test(t),
          mentionsFailed: /failed to fetch|could not reach|unavailable|network error|error loading/i.test(t),
          excerpt: t.replace(/\\n+/g,' | ').slice(0, 300),
        };
      })()`);
      out.errorStates.push({
        route,
        ...info,
        exceptions: cdp.exceptions.slice(0, 4),
        consoleErrors: cdp.console.filter((c) => c.type === "error").map((c) => c.text).slice(0, 4),
      });
    }
    await cdp.send("Network.setBlockedURLs", { urls: [] });

    log(JSON.stringify(out, null, 2));
  } catch (e) {
    out.error = String(e);
    log(JSON.stringify(out, null, 2));
    process.exitCode = 1;
  } finally {
    proc.kill();
  }
}

main();
