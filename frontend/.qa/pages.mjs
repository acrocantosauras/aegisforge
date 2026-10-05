// Authenticated sweep of every app page: real data presence, error states,
// console/network health, horizontal overflow at multiple widths, and an
// accessibility audit across every route.
//
// Usage: node .qa/pages.mjs   (requires the API + frontend to be running)
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const log = (...a) => console.log(...a);

const ROUTES = [
  "/dashboard",
  "/execute",
  "/history",
  "/agents",
  "/tools",
  "/documents",
  "/approvals",
  "/system",
  "/settings",
];
const WIDTHS = [
  [1440, 900, "1440"],
  [1280, 900, "1280"],
  [1024, 900, "1024"],
  [768, 1024, "768"],
];

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  const out = { pages: [], widths: {}, authGate: null, a11y: [], a11ySummary: {} };

  try {
    await cdp.setViewport(1440, 900);
    await cdp.navigate(`${ORIGIN}/login`, 3000);

    // --- Auth gate: protected page without token redirects to /login ---
    await cdp.evaluate("localStorage.clear()");
    await cdp.navigate(`${ORIGIN}/dashboard`, 3500);
    out.authGate = {
      urlAfterDashboard: await cdp.evaluate("location.pathname"),
      loginVisible: await cdp.evaluate(
        "!!document.querySelector('input[type=email],input[type=password]')"
      ),
    };

    // --- Seed a real token via the browser path ---
    await cdp.navigate(`${ORIGIN}/login`, 2500);
    const email = `qa25pages-${Date.now()}@example.com`;
    await cdp.evaluate(`(async () => {
      const email = ${JSON.stringify(email)};
      const password = 'Qa25Passw0rd!x';
      await fetch('/api/v1/auth/register', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password,full_name:'QA Pages'})});
      const r = await fetch('/api/v1/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
      const b = await r.json();
      localStorage.setItem('aegisforge_token', b.access_token);
      localStorage.setItem('aegisforge_email', email);
    })()`);

    // --- Seed one completed execution so the workspace is real data ---
    const intent =
      "Analyze the current support escalation policy and recommend improvements";
    const seeded = await cdp.evaluate(`(async () => {
      const t = localStorage.getItem('aegisforge_token');
      const h = {'Content-Type':'application/json', Authorization:'Bearer '+t};
      const r = await fetch('/api/v1/requests', {method:'POST',headers:h,body:JSON.stringify({intent:${JSON.stringify(intent)},context:{}})});
      const b = await r.json();
      await fetch('/api/v1/execution/requests/'+b.id+'/execute-async',{method:'POST',headers:{Authorization:'Bearer '+t}});
      return b.id;
    })()`);
    for (let i = 0; i < 60; i++) {
      const st = await cdp.evaluate(`(async () => {
        const t = localStorage.getItem('aegisforge_token');
        const r = await fetch('/api/v1/requests/${seeded}',{headers:{Authorization:'Bearer '+t}});
        return (await r.json()).status;
      })()`);
      if (st === "completed" || st === "failed") break;
      await sleep(2000);
    }
    const workspaceRoute = `/requests/${seeded}`;
    const allRoutes = [...ROUTES, workspaceRoute];

    // --- Per-route render sweep ---
    for (const route of allRoutes) {
      cdp.resetLogs();
      await cdp.navigate(`${ORIGIN}${route}`, 4500);
      const info = await cdp.evaluate(`(() => {
        const t = document.body.innerText || '';
        const errBoxes = Array.from(document.querySelectorAll('.error, [role=alert]')).map(e => e.innerText.slice(0,120));
        const hasSpinner = !!document.querySelector('.skeleton, .loading-line, [class*="loading"]');
        return {
          path: location.pathname,
          title: (document.querySelector('h1')||{}).innerText || '',
          textLen: t.length,
          errorTexts: errBoxes.filter(Boolean),
          mentionsFetchFail: /Failed to fetch|Could not reach|network/i.test(t),
          hasLoading: hasSpinner,
          excerpt: t.replace(/\\n+/g,' | ').slice(0, 260),
        };
      })()`);
      out.pages.push({
        route,
        ...info,
        consoleErrors: cdp.console.filter((c) => c.type === "error").map((c) => c.text).slice(0, 5),
        exceptions: cdp.exceptions.slice(0, 3),
        badResponses: cdp.responses.filter((r) => r.status >= 400 && !r.url.includes("favicon")).map(r => `${r.status} ${r.url.replace(ORIGIN,'')}`).slice(0, 8),
      });
    }

    // --- Responsive overflow checks on EVERY route ---
    for (const [w, h, label] of WIDTHS) {
      await cdp.setViewport(w, h);
      const res = {};
      for (const route of allRoutes) {
        await cdp.navigate(`${ORIGIN}${route}`, 2500);
        const m = await cdp.evaluate(`(() => {
          const de = document.documentElement;
          const overflow = de.scrollWidth - window.innerWidth;
          let worst = null, worstW = 0;
          for (const el of document.querySelectorAll('body *')) {
            const r = el.getBoundingClientRect();
            if (r.right > window.innerWidth + 1 && r.width > worstW) { worstW = r.width; worst = (el.className||el.tagName); }
          }
          return { overflow, worst: String(worst).slice(0,60), worstW: Math.round(worstW) };
        })()`);
        res[route] = m;
      }
      out.widths[label] = res;
    }

    // --- Accessibility audit across every route ---
    await cdp.setViewport(1440, 900);
    let totalUnnamed = 0;
    let totalUnlabeled = 0;
    for (const route of allRoutes) {
      await cdp.navigate(`${ORIGIN}${route}`, 3000);
      const a = await cdp.evaluate(`(() => {
        const buttons = Array.from(document.querySelectorAll('button'));
        const unnamed = buttons.filter(b => !(b.getAttribute('aria-label') || (b.innerText||'').trim() || b.getAttribute('title') || b.getAttribute('aria-labelledby'))).length;
        const inputs = Array.from(document.querySelectorAll('input, textarea, select'));
        const unlabeled = inputs.filter(i => {
          if (i.getAttribute('aria-label') || i.getAttribute('aria-labelledby')) return false;
          if (i.id && document.querySelector('label[for="'+i.id+'"]')) return false;
          if (i.closest('label')) return false;
          return true;
        }).map(i => i.tagName + (i.type?('['+i.type+']'):''));
        // headings: exactly one h1, no skipped levels
        const hs = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).map(h => Number(h.tagName[1]));
        let skips = [];
        for (let i = 1; i < hs.length; i++) if (hs[i] > hs[i-1] + 1) skips.push(hs[i-1]+'->'+hs[i]);
        // images without alt
        const imgsNoAlt = Array.from(document.querySelectorAll('img')).filter(i => !i.hasAttribute('alt')).length;
        // links without accessible name
        const links = Array.from(document.querySelectorAll('a[href]'));
        const unnamedLinks = links.filter(a => !(a.getAttribute('aria-label') || (a.innerText||'').trim() || a.querySelector('img[alt]:not([alt=""])'))).length;
        // landmark + skip link
        const hasMain = !!document.querySelector('main, #main-content');
        const focusables = document.querySelectorAll('a[href], button, input, textarea, select, [tabindex]:not([tabindex="-1"])').length;
        return { h1Count: hs.filter(x=>x===1).length, headingSkips: skips, unnamedButtons: unnamed, unlabeledInputs: unlabeled, imgsNoAlt, unnamedLinks, hasMain, focusables, buttonCount: buttons.length, linkCount: links.length };
      })()`);
      out.a11y.push({ route, ...a });
      totalUnnamed += a.unnamedButtons;
      totalUnlabeled += a.unlabeledInputs.length;
    }

    // --- Keyboard: first Tab lands on a visible focus target; Escape works ---
    await cdp.navigate(`${ORIGIN}/dashboard`, 3000);
    await cdp.evaluate("document.body.focus()");
    const focusTrail = [];
    for (let i = 0; i < 6; i++) {
      await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
      await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
      await sleep(120);
      focusTrail.push(await cdp.evaluate(`(() => {
        const el = document.activeElement;
        if (!el) return null;
        const cs = getComputedStyle(el);
        const visible = cs.outlineStyle !== 'none' || cs.boxShadow !== 'none';
        return { tag: el.tagName, name: (el.getAttribute('aria-label') || el.innerText || el.placeholder || '').slice(0,40), hasFocusRing: visible };
      })()`));
    }
    out.keyboard = { tabTrail: focusTrail };

    out.a11ySummary = {
      routesAudited: out.a11y.length,
      unnamedButtonsTotal: totalUnnamed,
      unlabeledInputsTotal: totalUnlabeled,
      routesMissingH1: out.a11y.filter(a => a.h1Count !== 1).map(a => a.route),
      routesWithHeadingSkips: out.a11y.filter(a => a.headingSkips.length > 0).map(a => `${a.route}:${a.headingSkips.join(",")}`),
      routesWithoutMainLandmark: out.a11y.filter(a => !a.hasMain).map(a => a.route),
      unnamedLinksTotal: out.a11y.reduce((n, a) => n + a.unnamedLinks, 0),
      imgsMissingAltTotal: out.a11y.reduce((n, a) => n + a.imgsNoAlt, 0),
    };

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
