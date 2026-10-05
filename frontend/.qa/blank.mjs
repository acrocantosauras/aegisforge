// Diagnostic: why does /requests/{id} render an empty body?
// Usage: REQUEST_ID=... ACCOUNT_EMAIL=... node .qa/blank.mjs
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const REQUEST_ID = process.env.REQUEST_ID || "";
const ACCOUNT_EMAIL = process.env.ACCOUNT_EMAIL || "";

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  cdp.resetLogs();
  try {
    await cdp.setViewport(1440, 900);
    await cdp.navigate(`${ORIGIN}/login`, 3000);
    const auth = await cdp.evaluate(`(async () => {
      const r = await fetch('/api/v1/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:${JSON.stringify(ACCOUNT_EMAIL)},password:'ProbePass123!'})});
      const b = await r.json().catch(() => ({}));
      if (b.access_token) {
        localStorage.setItem('aegisforge_token', b.access_token);
        localStorage.setItem('aegisforge_email', ${JSON.stringify(ACCOUNT_EMAIL)});
      }
      return { ok: !!b.access_token };
    })()`);
    console.log("auth:", JSON.stringify(auth));

    // Control: does a known-good page render?
    await cdp.navigate(`${ORIGIN}/dashboard`, 5000);
    const dash = await cdp.evaluate(`(() => ({
      bodyLen: document.body.innerText.length,
      hasRoot: !!document.querySelector('#__next'),
      rootChildren: document.querySelector('#__next') ? document.querySelector('#__next').children.length : -1,
      scripts: document.scripts.length,
      hasShell: !!document.querySelector('.shell'),
    }))()`);
    console.log("dashboard:", JSON.stringify(dash));

    // Target workspace route.
    await cdp.navigate(`${ORIGIN}/requests/${REQUEST_ID}`, 8000);
    await sleep(3000);
    const ws = await cdp.evaluate(`(() => ({
      path: location.pathname,
      bodyLen: document.body.innerText.length,
      bodyText: document.body.innerText.replace(/\\n+/g, ' | ').slice(0, 300),
      hasRoot: !!document.querySelector('#__next'),
      rootHTML: (document.querySelector('#__next')||{innerHTML:''}).innerHTML.slice(0,300),
      rootChildren: document.querySelector('#__next') ? document.querySelector('#__next').children.length : -1,
      scripts: document.scripts.length,
      hasShell: !!document.querySelector('.shell'),
      readyState: document.readyState,
    }))()`);
    console.log("workspace:", JSON.stringify(ws, null, 1));

    // Give hydration more time and re-check.
    await sleep(5000);
    const ws2 = await cdp.evaluate(`(() => ({
      bodyLen: document.body.innerText.length,
      bodyText: document.body.innerText.replace(/\\n+/g, ' | ').slice(0, 300),
      rootChildren: document.querySelector('#__next') ? document.querySelector('#__next').children.length : -1,
      hasShell: !!document.querySelector('.shell'),
    }))()`);
    console.log("workspace after 8s:", JSON.stringify(ws2));

    console.log("console errors:", JSON.stringify(cdp.console.filter((c) => c.type === "error").map((c) => c.text)));
    console.log("exceptions:", JSON.stringify(cdp.exceptions));
    console.log("failed responses:", JSON.stringify(cdp.responses.filter((r) => r.status >= 400).map((r) => `${r.status} ${r.url.replace(ORIGIN, "")}`)));
    console.log("failed requests:", JSON.stringify(cdp.failures.slice(0, 6)));
  } finally {
    proc.kill();
  }
}

main();
