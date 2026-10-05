// Verify the Tools & MCP page tabs render live (platform tools + MCP servers).
import { launchChrome, CDP, ORIGIN, sleep } from "./cdp.mjs";

async function main() {
  const { proc } = await launchChrome({ width: 1440, height: 900 });
  const cdp = await CDP.connect();
  cdp.resetLogs();
  try {
    await cdp.setViewport(1440, 900);
    await cdp.navigate(`${ORIGIN}/login`, 2500);
    const email = `qamcp-${Date.now()}@example.com`;
    const auth = await cdp.evaluate(`(async () => {
      const email = ${JSON.stringify(email)};
      const password = 'QaMcpPassw0rd!x';
      await fetch('/api/v1/auth/register', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password,full_name:'QA MCP'})}).catch(()=>{});
      const r = await fetch('/api/v1/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
      const b = await r.json();
      if (b.access_token) {
        localStorage.setItem('aegisforge_token', b.access_token);
        localStorage.setItem('aegisforge_email', email);
      }
      return { ok: !!b.access_token };
    })()`);
    if (!auth.ok) throw new Error("login failed");

    await cdp.navigate(`${ORIGIN}/tools`, 4500);
    const toolsTab = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      const tabs = Array.from(document.querySelectorAll('button.chip')).map(b => b.innerText.trim());
      return {
        tabs,
        activeIsTools: /Platform tools/.test(t),
        hasRiskBadge: /risk/.test(t),
        hasTimeout: /timeout/i.test(t),
        cards: document.querySelectorAll('.registry-card').length,
      };
    })()`);

    await cdp.evaluate(`(() => {
      const b = Array.from(document.querySelectorAll('button.chip')).find(x => /MCP servers/i.test(x.innerText));
      if (b) b.click();
      return !!b;
    })()`);
    await sleep(800);
    const mcpTab = await cdp.evaluate(`(() => {
      const t = document.body.innerText;
      const b = Array.from(document.querySelectorAll('button.chip')).find(x => /MCP servers/i.test(x.innerText));
      return {
        tabPressed: b ? b.getAttribute('aria-pressed') : null,
        mentionsMcp: /MCP/i.test(t),
        // No MCP server is configured in this dev stack — the page must show
        // the explanatory empty state rather than a blank panel.
        emptyState: /No MCP servers configured/i.test(t),
        hasTableOrEmpty: !!document.querySelector('table') || /No MCP servers configured/i.test(t),
        textLen: t.length,
      };
    })()`);

    const out = {
      toolsTab,
      mcpTab,
      consoleErrors: cdp.console.filter((c) => c.type === "error").map((c) => c.text),
      exceptions: cdp.exceptions,
      failedResponses: cdp.responses
        .filter((r) => r.status >= 400 && !r.url.includes("favicon"))
        .map((r) => `${r.status} ${r.url.replace(ORIGIN, "")}`),
    };
    console.log(JSON.stringify(out, null, 2));
    const ok =
      toolsTab.cards > 0 &&
      mcpTab.tabPressed === "true" &&
      mcpTab.hasTableOrEmpty &&
      out.consoleErrors.length === 0 &&
      out.exceptions.length === 0;
    proc.kill();
    process.exit(ok ? 0 : 1);
  } catch (e) {
    console.log(JSON.stringify({ error: String(e) }, null, 2));
    proc.kill();
    process.exit(1);
  }
}

main();
