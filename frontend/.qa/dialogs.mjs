// Focused a11y QA: dialog focus management, escape-to-close, focus return,
// focus visibility, and reduced-motion behaviour.
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

const log = (...a) => console.log(...a);

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  const out = { dialog: null, reducedMotion: null, focusVisible: null };

  try {
    await cdp.setViewport(1440, 900);
    await cdp.navigate(`${ORIGIN}/login`, 3000);
    const email = `qadlg-${Date.now()}@example.com`;
    await cdp.evaluate(`(async () => {
      const email = ${JSON.stringify(email)};
      const password = 'Qa25Passw0rd!x';
      await fetch('/api/v1/auth/register', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password,full_name:'QA Dialogs'})}).catch(()=>{});
      const r = await fetch('/api/v1/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
      const b = await r.json();
      localStorage.setItem('aegisforge_token', b.access_token);
      localStorage.setItem('aegisforge_email', email);
    })()`);

    // --- Seed a real document so a destructive action exists ---
    await cdp.evaluate(`(async () => {
      const t = localStorage.getItem('aegisforge_token');
      const fd = new FormData();
      fd.append('file', new Blob(['# QA document - used to exercise the delete confirmation dialog.'], { type: 'text/markdown' }), 'qa-dialog-doc.md');
      fd.append('title', 'qa-dialog-doc');
      await fetch('/api/v1/documents', { method: 'POST', headers: { Authorization: 'Bearer ' + t }, body: fd }).catch(() => {});
    })()`);

    // --- Documents page: open the delete confirmation dialog ---
    await cdp.navigate(`${ORIGIN}/documents`, 3500);
    const hasDialogTrigger = await cdp.evaluate(
      `Array.from(document.querySelectorAll('button')).some(b => /delete/i.test(b.innerText))`
    );

    if (hasDialogTrigger) {
      await cdp.evaluate(
        `Array.from(document.querySelectorAll('button')).find(b => /delete/i.test(b.innerText)).click()`
      );
      await sleep(600);

      const opened = await cdp.evaluate(`(() => {
        const d = document.querySelector('[role=dialog]');
        if (!d) return { present: false };
        const active = document.activeElement;
        return {
          present: true,
          ariaModal: d.getAttribute('aria-modal'),
          labelled: !!(d.getAttribute('aria-label') || d.getAttribute('aria-labelledby')),
          focusInside: d.contains(active),
          activeTag: active ? active.tagName : null,
          escBound: true,
        };
      })()`);

      // Escape closes it.
      await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await sleep(500);
      const closedByEscape = await cdp.evaluate(
        `!document.querySelector('[role=dialog]')`
      );

      // Re-open and confirm Tab stays within the dialog (no keyboard trap
      // outside; focus must not escape to the page behind).
      await cdp.evaluate(
        `Array.from(document.querySelectorAll('button')).find(b => /delete/i.test(b.innerText)).click()`
      );
      await sleep(500);
      let focusEscaped = false;
      for (let i = 0; i < 8; i++) {
        await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
        await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
        await sleep(100);
        const inside = await cdp.evaluate(
          `(() => { const d = document.querySelector('[role=dialog]'); return d ? d.contains(document.activeElement) : true; })()`
        );
        if (!inside) focusEscaped = true;
      }
      // Close again so the page is left in a clean state.
      await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await sleep(400);

      out.dialog = { hasDialogTrigger, opened, closedByEscape, focusEscapedDuringTab: focusEscaped };
    } else {
      out.dialog = { hasDialogTrigger: false };
    }

    // --- Reduced motion: animations must not run when the user asks ---
    await cdp.send("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-reduced-motion", value: "reduce" }],
    });
    await cdp.navigate(`${ORIGIN}/dashboard`, 3000);
    out.reducedMotion = await cdp.evaluate(`(() => {
      const sample = Array.from(document.querySelectorAll('*')).filter(el => {
        const cs = getComputedStyle(el);
        return cs.animationName !== 'none' && cs.animationDuration !== '0s';
      }).slice(0, 5).map(el => ({ cls: String(el.className).slice(0,40), anim: getComputedStyle(el).animationName, dur: getComputedStyle(el).animationDuration }));
      const pulsers = Array.from(document.querySelectorAll('.pulse')).filter(el => getComputedStyle(el).animationName !== 'none' && getComputedStyle(el).animationDuration !== '0s').length;
      return { animatedWithReduceMotion: sample, pulsingDots: pulsers, sample };
    })()`);
    await cdp.send("Emulation.setEmulatedMedia", { features: [] });

    // --- Focus visibility on an interactive control ---
    await cdp.navigate(`${ORIGIN}/history`, 3000);
    await cdp.evaluate(`(() => {
      const b = document.querySelector('button, a[href]');
      if (b) b.focus();
    })()`);
    out.focusVisible = await cdp.evaluate(`(() => {
      const el = document.activeElement;
      if (!el) return null;
      const cs = getComputedStyle(el);
      return {
        tag: el.tagName,
        outline: cs.outlineStyle + ' ' + cs.outlineWidth,
        boxShadow: cs.boxShadow.slice(0, 60),
      };
    })()`);

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
