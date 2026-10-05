"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowRight,
  Boxes,
  CircleCheck,
  GitBranch,
  Layers,
  LineChart,
  Lock,
  Network,
  Radar,
  RefreshCcw,
  ShieldCheck,
  Workflow,
} from "lucide-react";
import { BrandMark } from "@/components/shell/AppShell";
import { useAuth } from "@/lib/auth";

/* ------------------------------------------------------------------ */
/* Reveal-on-scroll (respects reduced motion via CSS)                  */
/* ------------------------------------------------------------------ */

function Reveal({
  children,
  delay = 0,
  className = "",
}: {
  children: React.ReactNode;
  delay?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // jsdom / very old browsers: show content immediately.
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting) {
          setVisible(true);
          io.disconnect();
        }
      },
      { threshold: 0.15 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      className={`reveal ${visible ? "visible" : ""} ${className}`}
      style={delay ? { transitionDelay: `${delay}ms` } : undefined}
    >
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Hero orchestration visual: PLANNER → parallel agents → synthesis    */
/* ------------------------------------------------------------------ */

function OrchestrationVisual() {
  return (
    <div className="lp-orb" aria-hidden="true">
      <svg viewBox="0 0 440 460" role="img" aria-label="Multi-agent orchestration diagram">
        <defs>
          <radialGradient id="coreGlow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.5" />
            <stop offset="100%" stopColor="#38bdf8" stopOpacity="0" />
          </radialGradient>
        </defs>

        {/* edges: planner → fan-out */}
        <path className="lp-orb-edge" d="M220 78 C 220 108, 82 118, 82 152" />
        <path className="lp-orb-edge" d="M220 78 L 220 152" />
        <path className="lp-orb-edge" d="M220 78 C 220 108, 358 118, 358 152" />

        {/* edges: fan-in → analysis */}
        <path className="lp-orb-edge" d="M82 218 C 82 252, 220 258, 220 286" />
        <path className="lp-orb-edge" d="M358 218 C 358 252, 220 258, 220 286" />

        {/* edges: analysis → synthesis → evaluation */}
        <path className="lp-orb-edge flow" d="M220 342 L 220 368" />
        <path className="lp-orb-edge flow" d="M220 424 L 220 442" />

        {/* request node */}
        <rect className="lp-orb-node" x="160" y="12" width="120" height="34" rx="5" />
        <text className="lp-orb-label dim" x="220" y="33">REQUEST</text>

        {/* planner node */}
        <g className="lp-orb-core">
          <circle cx="220" cy="52" r="0" fill="url(#coreGlow)" />
          <rect className="lp-orb-node accent" x="152" y="44" width="136" height="34" rx="5" />
        </g>
        <text className="lp-orb-label accent" x="220" y="65">PLANNER</text>

        {/* parallel agents */}
        <rect className="lp-orb-node" x="32" y="152" width="100" height="66" rx="5" />
        <text className="lp-orb-label" x="82" y="180">RESEARCH</text>
        <text className="lp-orb-label dim" x="82" y="198">tools</text>

        <rect className="lp-orb-node" x="170" y="152" width="100" height="66" rx="5" />
        <text className="lp-orb-label" x="220" y="180">RAG</text>
        <text className="lp-orb-label dim" x="220" y="198">knowledge</text>

        <rect className="lp-orb-node" x="308" y="152" width="100" height="66" rx="5" />
        <text className="lp-orb-label" x="358" y="180">ANALYSIS</text>
        <text className="lp-orb-label dim" x="358" y="198">evidence</text>

        {/* synthesis */}
        <rect className="lp-orb-node accent" x="158" y="286" width="124" height="56" rx="5" />
        <text className="lp-orb-label" x="220" y="312">SYNTHESIS</text>
        <text className="lp-orb-label dim" x="220" y="330">grounded</text>

        {/* evaluation */}
        <rect className="lp-orb-node" x="164" y="368" width="112" height="56" rx="5" />
        <text className="lp-orb-label" x="220" y="394">EVALUATION</text>
        <text className="lp-orb-label dim" x="220" y="412">scored</text>

        {/* result */}
        <rect className="lp-orb-node" x="176" y="442" width="88" height="16" rx="4" />
        <text className="lp-orb-label accent" x="220" y="454" style={{ fontSize: 7 }}>
          VALIDATED RESULT
        </text>
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Data                                                                */
/* ------------------------------------------------------------------ */

const CAPABILITIES = [
  {
    icon: <Workflow size={17} />,
    title: "Intelligent Planning",
    text: "Requests are decomposed into validated execution plans — dependency-aware task graphs, not freeform chains.",
    meta: "dag · validated · waves",
  },
  {
    icon: <Boxes size={17} />,
    title: "Multi-Agent Execution",
    text: "Specialized research, analysis, and synthesis agents run in bounded parallel waves with per-task retries.",
    meta: "6 agent types · parallel",
  },
  {
    icon: <Layers size={17} />,
    title: "Enterprise Knowledge",
    text: "Hybrid retrieval over your documents with citations — agents answer from your corpus, not from thin air.",
    meta: "rag · pgvector · citations",
  },
  {
    icon: <Radar size={17} />,
    title: "Tool Ecosystem",
    text: "MCP tools with permission checks, risk classification, and deny-by-default execution boundaries.",
    meta: "mcp · permissions · risk",
  },
  {
    icon: <CircleCheck size={17} />,
    title: "Evaluation & Critique",
    text: "Deterministic evaluators and LLM critics score planning, collaboration, and response quality.",
    meta: "measured · scored",
  },
  {
    icon: <RefreshCcw size={17} />,
    title: "Durable Workflows",
    text: "Checkpointed state survives restarts. Failures resume from the last checkpoint, not from zero.",
    meta: "checkpoint · resume",
  },
  {
    icon: <LineChart size={17} />,
    title: "Observability",
    text: "Every workflow exposes task state, durations, tool calls, and evaluation — audit-ready by default.",
    meta: "metrics · traces · audit",
  },
  {
    icon: <Lock size={17} />,
    title: "Security",
    text: "Tenant isolation, JWT auth, permission gates, and human approval for high-risk actions.",
    meta: "isolated · approved",
  },
];

const PIPELINE_STAGES: { key: string; name: string; desc: string }[] = [
  { key: "REQUEST", name: "REQUEST", desc: "Operator submits intent" },
  { key: "PLANNER", name: "PLANNER", desc: "Validated task graph" },
  { key: "AGENTS", name: "AGENTS", desc: "Research · RAG · Analysis in parallel" },
  { key: "SYNTHESIS", name: "SYNTHESIS", desc: "Grounded, cited answer" },
  { key: "EVALUATION", name: "EVALUATION", desc: "Scored before delivery" },
  { key: "RESULT", name: "RESULT", desc: "Evidence attached" },
];

const RELIABILITY_STEPS = [
  {
    title: "Checkpoint",
    text: "Workflow state is persisted after every node — plan, task records, tool calls, evidence.",
  },
  {
    title: "Failure",
    text: "A tool times out or a worker dies mid-run. The failure is recorded, not silently swallowed.",
  },
  {
    title: "Recovery",
    text: "Stuck-job detection and bounded retries re-queue work; circuit breakers fail fast around broken tools.",
  },
  {
    title: "Resume",
    text: "Execution continues from the durable checkpoint — completed tasks are never re-run.",
  },
  {
    title: "Completed",
    text: "The workflow finishes with full audit trail and evaluation, regardless of what broke along the way.",
  },
];

const STAGES: { key: string }[] = [
  { key: "REQUEST" },
  { key: "PLANNER" },
  { key: "AGENTS" },
  { key: "TOOLS" },
  { key: "EVALUATION" },
  { key: "RESULT" },
];

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

const NAV_LINKS = [
  { href: "#platform", label: "Platform" },
  { href: "#workflow", label: "Workflow" },
  { href: "#capabilities", label: "Capabilities" },
  { href: "#reliability", label: "Reliability" },
  { href: "#architecture", label: "Architecture" },
];

export default function LandingPage() {
  const { isAuthenticated, isLoading } = useAuth();
  const [scrolled, setScrolled] = useState(false);
  const [activeStage, setActiveStage] = useState("AGENTS");

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Rotate the pipeline highlight — communicates the flow direction.
  useEffect(() => {
    const t = setInterval(() => {
      setActiveStage((cur) => {
        const i = STAGES.findIndex((s) => s.key === cur);
        return STAGES[(i + 1) % STAGES.length].key;
      });
    }, 2200);
    return () => clearInterval(t);
  }, []);

  const workspaceHref = isAuthenticated ? "/dashboard" : "/login";

  return (
    <div className="lp">
      {/* ---------------- Nav ---------------- */}
      <nav className={`lp-nav ${scrolled ? "scrolled" : ""}`} aria-label="Main">
        <div className="lp-nav-inner">
          <Link href="/" className="brand">
            <BrandMark />
            <span className="brand-name">
              AEGIS<span className="thin">FORGE</span>
            </span>
          </Link>
          <div className="lp-links">
            {NAV_LINKS.map((l) => (
              <a key={l.href} href={l.href}>
                {l.label}
              </a>
            ))}
          </div>
          <div className="lp-nav-actions">
            <Link href="/login">
              <button className="ghost">Sign in</button>
            </Link>
            <Link href={workspaceHref}>
              <button className="primary">
                Launch Workspace <ArrowRight size={14} />
              </button>
            </Link>
          </div>
        </div>
      </nav>

      {/* ---------------- Hero ---------------- */}
      <header className="lp-hero">
        <div className="lp-hero-bg" aria-hidden="true">
          <div className="lp-hero-grid" />
          <div className="lp-hero-glow" />
        </div>
        <div className="lp-wrap">
          <div className="lp-hero-inner">
            <div className="lp-hero-copy">
              <span className="lp-eyebrow">
                <span className="status-dot ok pulse" />
                Enterprise AI orchestration
              </span>
              <h1>
                AI orchestration for complex work.
              </h1>
              <p className="lp-hero-sub">
                Plan, execute, evaluate, and recover multi-agent workflows across
                enterprise knowledge, tools, and data — with durable checkpoints,
                permission gates, and full observability.
              </p>
              <div className="lp-hero-ctas">
                <Link href={workspaceHref}>
                  <button className="primary cta">
                    Launch AegisForge <ArrowRight size={15} />
                  </button>
                </Link>
                <a href="#architecture">
                  <button className="cta ghost">Explore Architecture</button>
                </a>
              </div>
              <div className="lp-hero-meta">
                <span>
                  <GitBranch size={13} /> dependency-aware planning
                </span>
                <span>
                  <ShieldCheck size={13} /> human approval gates
                </span>
                <span>
                  <RefreshCcw size={13} /> checkpoint & resume
                </span>
              </div>
            </div>
            <OrchestrationVisual />
          </div>
        </div>
      </header>

      {/* ---------------- Capability strip ---------------- */}
      <section className="lp-caps" aria-label="Platform capabilities">
        <div className="lp-wrap">
          <div className="lp-caps-row">
            {[
              "Multi-Agent",
              "RAG",
              "MCP",
              "Durable Workflows",
              "Evaluation",
              "Observability",
              "Secure Execution",
            ].map((c) => (
              <span key={c} className="lp-cap">
                <Network size={13} />
                {c}
              </span>
            ))}
          </div>
        </div>
      </section>

      {/* ---------------- From request to result ---------------- */}
      <section className="lp-section" id="workflow">
        <div className="lp-wrap">
          <Reveal>
            <div className="lp-section-head">
              <span className="t-caps">Execution model</span>
              <h2>From request to result</h2>
              <p>
                Every request becomes a validated task graph. Agents execute in
                parallel waves, pass evidence downstream, and every claim in the
                final answer carries its citations.
              </p>
            </div>
          </Reveal>
          <Reveal delay={80}>
            <div className="lp-pipeline" role="list">
              {PIPELINE_STAGES.map((s) => (
                <div key={s.name} className="lp-pipe-stage" role="listitem">
                  <div
                    className="lp-pipe-node"
                    style={
                      activeStage === s.key
                        ? { borderColor: "var(--accent-border)", boxShadow: "var(--shadow-glow)" }
                        : undefined
                    }
                  >
                    <div className="name">{s.name}</div>
                    <div className="desc">{s.desc}</div>
                  </div>
                  <span className="lp-pipe-connector" aria-hidden="true" />
                </div>
              ))}
            </div>
          </Reveal>
        </div>
      </section>

      {/* ---------------- Platform capabilities ---------------- */}
      <section className="lp-section" id="capabilities" style={{ paddingTop: 0 }}>
        <div className="lp-wrap">
          <Reveal>
            <div className="lp-section-head">
              <span className="t-caps">Platform</span>
              <h2>Built for work that has to hold up</h2>
              <p>
                Not a chat wrapper. A control plane: every capability below is a
                real subsystem in the execution path.
              </p>
            </div>
          </Reveal>
          <div className="lp-grid">
            {CAPABILITIES.map((c, i) => (
              <Reveal key={c.title} delay={(i % 4) * 60}>
                <div className="lp-card">
                  <div className="lp-card-icon">{c.icon}</div>
                  <h3>{c.title}</h3>
                  <p>{c.text}</p>
                  <span className="meta">{c.meta}</span>
                </div>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* ---------------- Reliability ---------------- */}
      <section className="lp-section" id="reliability" style={{ paddingTop: 0 }}>
        <div className="lp-wrap">
          <Reveal>
            <div className="lp-section-head">
              <span className="t-caps">Durability</span>
              <h2>Workflows that don&apos;t die when infrastructure does</h2>
              <p>
                AegisForge treats failure as a scheduled event. Checkpointed
                state, bounded retries, stuck-job detection, and circuit
                breakers turn crashes into pauses.
              </p>
            </div>
          </Reveal>
          <div className="lp-reliability">
            <Reveal>
              <div className="lp-relo-steps">
                {RELIABILITY_STEPS.map((s, i) => (
                  <div key={s.title} className={`lp-relo-step ${i === 2 ? "hot" : ""}`}>
                    <span className="lp-relo-dot">{String(i + 1).padStart(2, "0")}</span>
                    <div className="lp-relo-body">
                      <h3>{s.title}</h3>
                      <p>{s.text}</p>
                    </div>
                  </div>
                ))}
              </div>
            </Reveal>
            <Reveal delay={100}>
              <div className="lp-relo-visual" aria-label="Recovery example timeline">
                <div className="line">
                  <span className="k">wf-7f3a · checkpoint</span>
                  <span className="v ok">saved</span>
                </div>
                <div className="line">
                  <span className="k">task rag-2 · tool timeout</span>
                  <span className="v err">failed</span>
                </div>
                <div className="line">
                  <span className="k">retry policy · backoff</span>
                  <span className="v retry">retry 1/2</span>
                </div>
                <div className="line">
                  <span className="k">worker resumed</span>
                  <span className="v ok">from checkpoint</span>
                </div>
                <div className="line">
                  <span className="k">wave 3 · synthesis</span>
                  <span className="v ok">running</span>
                </div>
                <div className="line">
                  <span className="k">workflow</span>
                  <span className="v ok">completed</span>
                </div>
              </div>
            </Reveal>
          </div>
        </div>
      </section>

      {/* ---------------- Observability ---------------- */}
      <section className="lp-section" style={{ paddingTop: 0 }}>
        <div className="lp-wrap">
          <Reveal>
            <div className="lp-section-head">
              <span className="t-caps">Observability</span>
              <h2>The state of every run, on one screen</h2>
              <p>
                Workers, queue depth, retries, failure rates, and tool health —
                measured from the live control plane, not asserted. The panel
                below is an illustrative view of the real dashboard.
              </p>
            </div>
          </Reveal>
          <Reveal delay={80}>
            <div className="lp-telemetry">
              <div className="lp-telemetry-head">
                <span>cluster · live view</span>
                <span className="row" style={{ gap: 8 }}>
                  <span className="status-dot ok pulse" /> streaming
                </span>
              </div>
              <div className="lp-telemetry-body">
                <div className="lp-tel-cell ok">
                  <div className="k">Workers</div>
                  <div className="v">
                    2 <span className="unit">online</span>
                  </div>
                </div>
                <div className="lp-tel-cell">
                  <div className="k">Queue</div>
                  <div className="v">
                    14 <span className="unit">jobs</span>
                  </div>
                </div>
                <div className="lp-tel-cell">
                  <div className="k">Active tasks</div>
                  <div className="v">
                    7 <span className="unit">running</span>
                  </div>
                </div>
                <div className="lp-tel-cell ok">
                  <div className="k">Recovery</div>
                  <div className="v">
                    0 <span className="unit">needed</span>
                  </div>
                </div>
                <div className="lp-tel-cell warn">
                  <div className="k">Failure rate</div>
                  <div className="v">
                    0.4<span className="unit">%</span>
                  </div>
                </div>
              </div>
              <div className="lp-spark">
                {[
                  { label: "execution latency", pct: 62, val: "2.4s p50", cls: "ok" },
                  { label: "tool success", pct: 96, val: "99.6%", cls: "ok" },
                  { label: "retry pressure", pct: 12, val: "low", cls: "" },
                  { label: "queue wait", pct: 34, val: "1.1s", cls: "" },
                  { label: "eval pass rate", pct: 88, val: "0.88", cls: "ok" },
                ].map((r, i) => (
                  <div key={r.label} className="lp-spark-row">
                    <span>{r.label}</span>
                    <span className="lp-spark-bar">
                      <i style={{ width: `${r.pct}%`, animationDelay: `${i * 120}ms` }} />
                    </span>
                    <span className={`val ${r.cls}`}>{r.val}</span>
                  </div>
                ))}
              </div>
            </div>
          </Reveal>
        </div>
      </section>

      {/* ---------------- Architecture ---------------- */}
      <section className="lp-section" id="architecture" style={{ paddingTop: 0 }}>
        <div className="lp-wrap">
          <Reveal>
            <div className="lp-section-head">
              <span className="t-caps">Architecture</span>
              <h2>Every layer, engineered for the failure case</h2>
            </div>
          </Reveal>
          <Reveal delay={60}>
            <div className="lp-arch">
              {[
                { name: "Browser", role: "Next.js control plane UI" },
                { name: "Next.js", role: "Server-rendered workspace" },
                { name: "FastAPI", role: "JWT auth · tenant isolation · approvals" },
                { name: "LangGraph", role: "Checkpointed multi-agent execution" },
                { name: "Agents", role: "Plan · Research · RAG · Analyze · Synthesize · Evaluate" },
                { name: "Tools / MCP / RAG", role: "Permissioned tool & knowledge boundary" },
                { name: "PostgreSQL / pgvector · Redis", role: "Durable state · vectors · job queue" },
              ].map((l) => (
                <div key={l.name} style={{ display: "contents" }}>
                  <div className="lp-arch-layer">
                    <span className="name">{l.name}</span>
                    <span className="role">{l.role}</span>
                  </div>
                  <span className="lp-arch-arrow" aria-hidden="true" />
                </div>
              ))}
            </div>
          </Reveal>
          <Reveal delay={120}>
            <div className="lp-arch-side">
              {[
                { icon: <LineChart size={13} />, label: "Observability" },
                { icon: <Lock size={13} />, label: "Security" },
                { icon: <CircleCheck size={13} />, label: "Evaluation" },
                { icon: <RefreshCcw size={13} />, label: "Checkpointing" },
              ].map((s) => (
                <span key={s.label} className="lp-arch-chip">
                  <span className="row" style={{ gap: 7, justifyContent: "center" }}>
                    {s.icon}
                    {s.label}
                  </span>
                </span>
              ))}
            </div>
          </Reveal>
        </div>
      </section>

      {/* ---------------- Final CTA ---------------- */}
      <section className="lp-cta">
        <div className="lp-wrap">
          <Reveal>
            <h2>Build workflows that don&apos;t stop at generation.</h2>
            <p>
              AegisForge runs your work through planning, execution, evaluation,
              and recovery — and shows you every step.
            </p>
            <Link href={workspaceHref}>
              <button className="primary cta">
                Launch AegisForge <ArrowRight size={15} />
              </button>
            </Link>
          </Reveal>
        </div>
      </section>

      {/* ---------------- Footer ---------------- */}
      <footer className="lp-footer">
        <div className="lp-wrap">
          <div className="lp-footer-inner">
            <span className="brand" style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
              <BrandMark size={20} />
              <span className="brand-name" style={{ fontSize: "0.8125rem" }}>
                AEGIS<span className="thin">FORGE</span>
              </span>
            </span>
            <span className="note">
              Enterprise multi-agent orchestration · © 2026 AegisForge
            </span>
          </div>
        </div>
      </footer>
    </div>
  );
}
