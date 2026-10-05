// Phase 2.5 visual QA: real backend data, real headless Chrome, four widths.
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const log = (...a) => console.log(...a);

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  const out = { shots: [], overflow: {} };

  try {
    await cdp.setViewport(1440, 900);

    // --- anonymous landing + login shots ---
    await cdp.navigate(`${ORIGIN}/`, 4000);
    await cdp.screenshot("./.qa/shot-landing-1440.png");
    await cdp.navigate(`${ORIGIN}/login`, 3000);
    await cdp.screenshot("./.qa/shot-login-1440.png");

    // --- real account + real execution ---
    const email = `qa25shots-${Date.now()}@example.com`;
    const password = "Qa25Passw0rd!x";
    await cdp.evaluate(`(async () => {
      await fetch('/api/v1/auth/register', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({email:${JSON.stringify(email)},password:${JSON.stringify(password)},full_name:'QA Shots'})});
      const r = await fetch('/api/v1/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({email:${JSON.stringify(email)},password:${JSON.stringify(password)}})});
      const b = await r.json();
      localStorage.setItem('aegisforge_token', b.access_token);
      localStorage.setItem('aegisforge_email', ${JSON.stringify(email)});
    })()`);

    const intent =
      "Produce a rollout plan for the internal security policy onboarding controls, with owners and verification steps.";
    const created = await cdp.evaluate(`(async () => {
      const t = localStorage.getItem('aegisforge_token');
      const h = {'Content-Type':'application/json', Authorization:'Bearer '+t};
      const r = await fetch('/api/v1/requests', {method:'POST',headers:h,body:JSON.stringify({intent:${JSON.stringify(intent)},context:{}})});
      const b = await r.json();
      await fetch('/api/v1/execution/requests/'+b.id+'/execute-async',{method:'POST',headers:{Authorization:'Bearer '+t}});
      return b.id;
    })()`);

    let wfStatus = "";
    for (let i = 0; i < 60; i++) {
      await sleep(2000);
      wfStatus = await cdp.evaluate(`(async () => {
        const t = localStorage.getItem('aegisforge_token');
        const r = await fetch('/api/v1/workflows/by-request/${created}',{headers:{Authorization:'Bearer '+t}});
        if (!r.ok) return 'err';
        return (await r.json()).status;
      })()`);
      if (["completed", "failed", "cancelled"].includes(wfStatus)) break;
    }
    out.requestId = created;
    out.wfStatus = wfStatus;

    const routes = [
      ["dashboard", "/dashboard"],
      ["execute", "/execute"],
      ["workspace", `/requests/${created}`],
      ["history", "/history"],
      ["system", "/system"],
      ["agents", "/agents"],
      ["tools", "/tools"],
      ["documents", "/documents"],
      ["approvals", "/approvals"],
      ["settings", "/settings"],
    ];

    for (const [width, height, label] of [
      [1440, 900, "1440"],
      [1280, 900, "1280"],
      [1024, 900, "1024"],
      [768, 1024, "768"],
    ]) {
      await cdp.setViewport(width, height);
      out.overflow[label] = {};
      for (const [name, route] of routes) {
        await cdp.navigate(`${ORIGIN}${route}`, 2600);
        const m = await cdp.evaluate(`(() => {
          const de = document.documentElement;
          let worst = null, worstRight = 0;
          for (const el of document.querySelectorAll('body *')) {
            const r = el.getBoundingClientRect();
            if (r.right > window.innerWidth + 1 && r.right > worstRight) {
              worstRight = r.right;
              worst = (el.tagName + '.' + String(el.className||'')).slice(0, 70);
            }
          }
          return { overflow: de.scrollWidth - window.innerWidth, worst };
        })()`);
        out.overflow[label][name] = m;
        // Full-resolution shots for the centerpiece + representative pages
        if (["workspace", "dashboard", "execute", "history"].includes(name)) {
          await cdp.screenshot(`./.qa/shot-${name}-${label}.png`);
        }
      }
    }

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
