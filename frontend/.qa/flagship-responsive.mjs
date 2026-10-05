// Responsive + a11y sweep of the flagship execution workspace.
// Uses the most recent completed flagship run for the demo account.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WIDTHS = [1440, 1280, 1024, 768];

async function main() {
  const email = process.env.DEMO_EMAIL;
  const requestId = process.env.DEMO_REQUEST_ID;
  if (!email || !requestId) {
    console.error("set DEMO_EMAIL and DEMO_REQUEST_ID");
    process.exit(1);
  }
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  const out = { widths: [], errors: [] };

  try {
    await cdp.setViewport(1440, 900);
    await cdp.navigate(`${ORIGIN}/login`, 2500);
    const loggedIn = await cdp.evaluate(`(async () => {
      const r = await fetch('/api/v1/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: ${JSON.stringify(email)}, password: ${JSON.stringify(process.env.DEMO_PASSWORD || 'FlagshipDemo1!x')} }),
      });
      if (!r.ok) return { ok: false, status: r.status };
      const b = await r.json();
      localStorage.setItem('aegisforge_token', b.access_token);
      return { ok: true };
    })()`);
    if (!loggedIn.ok) throw new Error("login failed: " + JSON.stringify(loggedIn));

    for (const w of WIDTHS) {
      await cdp.setViewport(w, 900);
      await cdp.navigate(`${ORIGIN}/requests/${requestId}`, 6000);
      await sleep(1800);
      const m = await cdp.evaluate(`(() => {
        const docW = document.documentElement.clientWidth;
        const overflow = document.documentElement.scrollWidth > docW + 1;
        const offenders = [];
        document.querySelectorAll('*').forEach(el => {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && (r.right > docW + 2 || r.left < -2)) {
            offenders.push(el.className && typeof el.className === 'string' ? el.className : el.tagName);
          }
        });
        return {
          docW, scrollW: document.documentElement.scrollWidth, overflow,
          offenders: Array.from(new Set(offenders)).slice(0, 6),
          taskNodes: document.querySelectorAll('[data-task-id]').length,
          edges: document.querySelectorAll('svg path').length,
          citations: document.querySelectorAll('.citation-row').length,
          hasResult: /Final Result/i.test(document.body.innerText),
          hasEval: /Evaluation/i.test(document.body.innerText),
          graphVisible: !!document.querySelector('.task-graph')?.getBoundingClientRect().height,
          buttons: document.querySelectorAll('button').length,
          unnamedButtons: Array.from(document.querySelectorAll('button')).filter(b => !b.innerText.trim() && !b.getAttribute('aria-label')).length,
          unlabeledInputs: Array.from(document.querySelectorAll('input, textarea, select')).filter(i => !i.labels?.length && !i.getAttribute('aria-label')).length,
          h1: document.querySelectorAll('h1').length,
        };
      })()`);
      out.widths.push({ width: w, ...m });
      if (m.overflow) out.errors.push(`overflow at ${w}px: ${m.offenders.join(', ')}`);
      if (m.taskNodes !== 4 || m.citations < 4) out.errors.push(`workspace degraded at ${w}px`);
      await cdp.screenshot(path.join(HERE, `flagship-${w}.png`));
    }

    out.consoleErrors = cdp.console.filter((c) => c.type === "error").map((c) => c.text);
    console.log(JSON.stringify(out, null, 2));
    proc.kill();
    process.exit(out.errors.length ? 1 : 0);
  } catch (e) {
    console.log(JSON.stringify({ ...out, fatal: String(e && e.stack || e) }, null, 2));
    proc.kill();
    process.exit(1);
  }
}
main();
