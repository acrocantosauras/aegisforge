// Temporary QA harness: drive real headless Chrome via the DevTools Protocol.
// No mocks. Used only for Phase 2.5 validation; removed before finishing.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

export const CHROME =
  process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";
export const PORT = Number(process.env.CDP_PORT || 9333);
export const ORIGIN = process.env.QA_ORIGIN || "http://localhost:3131";

export async function launchChrome({ width = 1440, height = 900 } = {}) {
  const userDataDir = new URL("./profile", import.meta.url).pathname.replace(/^\//, "");
  mkdirSync(userDataDir, { recursive: true });
  const proc = spawn(
    CHROME,
    [
      `--remote-debugging-port=${PORT}`,
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--disable-background-networking",
      "--disable-sync",
      "--hide-scrollbars",
      `--window-size=${width},${height}`,
      `--user-data-dir=${userDataDir}`,
      "about:blank",
    ],
    { stdio: "ignore" }
  );
  const base = `http://127.0.0.1:${PORT}`;
  let ok = false;
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`${base}/json/version`);
      if (r.ok) {
        ok = true;
        break;
      }
    } catch {}
    await sleep(250);
  }
  if (!ok) throw new Error("Chrome DevTools endpoint did not come up");
  return { proc, base };
}

export class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.listeners = new Set();
    this.console = [];
    this.exceptions = [];
    this.logs = [];
    this.requests = [];
    this.responses = [];
    this.failures = [];
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { res, rej } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) rej(new Error(JSON.stringify(msg.error)));
        else res(msg.result);
        return;
      }
      if (msg.method === "Runtime.consoleAPICalled") {
        const text = (msg.params.args || [])
          .map((a) => a.value ?? a.description ?? "")
          .join(" ");
        this.console.push({ type: msg.params.type, text });
      } else if (msg.method === "Runtime.exceptionThrown") {
        this.exceptions.push(
          msg.params.exceptionDetails?.exception?.description ||
            msg.params.exceptionDetails?.text ||
            "exception"
        );
      } else if (msg.method === "Log.entryAdded") {
        this.logs.push({
          level: msg.params.entry.level,
          text: msg.params.entry.text,
          source: msg.params.entry.source,
        });
      } else if (msg.method === "Network.requestWillBeSent") {
        this.requests.push({
          url: msg.params.request.url,
          method: msg.params.request.method,
        });
      } else if (msg.method === "Network.responseReceived") {
        this.responses.push({
          url: msg.params.response.url,
          status: msg.params.response.status,
        });
      } else if (msg.method === "Network.loadingFailed") {
        this.failures.push({
          url: msg.params.requestId,
          error: msg.params.errorText,
        });
      }
      for (const fn of this.listeners) fn(msg);
    };
  }

  static async connect() {
    const base = `http://127.0.0.1:${PORT}`;
    let target;
    for (let i = 0; i < 40; i++) {
      const list = await (await fetch(`${base}/json/list`)).json();
      target = list.find((t) => t.type === "page");
      if (target) break;
      await sleep(250);
    }
    if (!target) throw new Error("no page target");
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.onopen = res;
      ws.onerror = rej;
    });
    const cdp = new CDP(ws);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Log.enable");
    await cdp.send("Network.enable");
    return cdp;
  }

  send(method, params = {}) {
    return new Promise((res, rej) => {
      const id = ++this.id;
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  waitEvent(method, timeoutMs = 30000) {
    return new Promise((res, rej) => {
      const to = setTimeout(() => {
        this.listeners.delete(fn);
        rej(new Error(`timeout waiting ${method}`));
      }, timeoutMs);
      const fn = (msg) => {
        if (msg.method === method) {
          clearTimeout(to);
          this.listeners.delete(fn);
          res(msg.params);
        }
      };
      this.listeners.add(fn);
    });
  }

  async navigate(url, settleMs = 2500) {
    const loaded = this.waitEvent("Page.loadEventFired", 45000).catch(() => null);
    await this.send("Page.navigate", { url });
    await loaded;
    await sleep(settleMs);
  }

  async evaluate(expression) {
    const r = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(
        r.exceptionDetails.exception?.description || r.exceptionDetails.text
      );
    }
    return r.result.value;
  }

  async setViewport(width, height, mobile = false) {
    await this.send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile,
    });
    await this.send("Emulation.setVisibleSize", { width, height });
  }

  async screenshot(path) {
    const r = await this.send("Page.captureScreenshot", {
      format: "png",
      captureBeyondViewport: true,
    });
    writeFileSync(path, Buffer.from(r.data, "base64"));
    return path;
  }

  resetLogs() {
    this.console = [];
    this.exceptions = [];
    this.logs = [];
    this.requests = [];
    this.responses = [];
    this.failures = [];
  }
}

export { sleep };
