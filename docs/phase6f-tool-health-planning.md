# Phase 6F — Tool Health-Aware Planning

## Motivation

Before 6F, the planner had **zero** availability context: the deterministic
planner always scheduled `knowledge.search`, and the LLM planner received no
tool-state information at all. Meanwhile, tool failures were classified in the
executor (`_classify_failure`) but the availability signal was discarded.
A tool that had failed repeatedly would be scheduled again and again.

6F closes that loop with a **bounded, deterministic, evidence-derived** health
model. Health is a **planning signal, not an execution guarantee**: a healthy
tool can still fail, and an unavailable tool can still be selected when no
alternative exists (execution then follows the normal permission, registry,
retry, and recovery paths — health never bypasses any of them).

## Audit findings (pre-6F)

| Question | Answer |
|---|---|
| Where tool health exists | Nowhere at tool level. MCP *server* health existed in `MCPServerCatalog`/`MCPLifecycleManager` (Phase 5). |
| Where failures are classified | `MultiAgentExecutor._classify_failure` / `_suggest_recovery` — signal discarded after logging. |
| Where failure history is stored | Nowhere (single-shot retries only). |
| Process-local or durable? | Process-local (in-memory ring). Deliberate: health is recent-evidence based, not a durable fact. |
| MCP server health exists? | Yes — lifecycle manager + `mcp_server_status_total` metrics. |
| Individual tool health existed? | No. |
| Planner tool context? | None. |

## Design

### Health model (`src/aegisforge/tools/health.py`)

States: `unknown | healthy | degraded | unavailable` (`ToolHealthState`).

Evidence: a fixed-size ring (default 20) of per-tool outcomes
(`success | failure | timeout`) recorded by `ToolRegistry.execute()` — the
single choke point every real tool invocation passes through. Derivation is a
pure function (`_derive_state`) of the window + prior state:

- **unknown** — fewer than `min_samples` (default 3) outcomes.
- **unavailable** — `unavailable_consecutive_failures` (default 4) consecutive
  failures, or failure rate ≥ `unavailable_failure_rate` (default 0.75) over
  the window.
- **degraded** — `degraded_consecutive_failures` (default 2) consecutive
  failures, or rate ≥ `degraded_failure_rate` (default 0.34).
- **healthy** — enough evidence with a low failure rate.
- **Sticky unavailable + graduated recovery** — leaving `unavailable` requires
  `recovery_samples` (default 2) consecutive successes, and lands in
  `degraded` if the window rate is still elevated; continued successes return
  the tool to `healthy`. A single transient failure can never mark a tool
  unavailable, and recovery is earned with evidence, not instantaneous.

All thresholds live in `Settings` (`tool_health_*` fields) and are validated
by `ToolHealthConfig.__post_init__`. The process-wide default tracker
(`get_default_tool_health_tracker()`) is shared by every `ToolRegistry` so
evidence accumulates across workflow nodes; registries are still cheap to
build per node.

### Planner-safe exposure

`ToolHealthSnapshot` is a frozen dataclass carrying only:
`tool_name, state, recent_failure_rate, recent_timeout_rate,
consecutive_failures, sample_count`. No errors, no payloads, no tenant or user
data. Planners can read but never write — there is no mutation path from
planning code to the tracker.

### MCP: server health ≠ tool health

- Tool health comes only from real tool executions.
- MCP server health remains exclusively owned by the lifecycle manager.
  `MCPLifecycleManager.server_health_summary()` exposes a read-only,
  side-effect-free `{server_id, state}` snapshot (no polling, no errors) used
  as additional LLM-planner context.
- A failing tool never marks its server unhealthy; a healthy server says
  nothing about individual tools.

## Planner integration

### Deterministic planner

`PlannerAgent._generate_plan` accepts bounded `input_data`:

- `tool_health` — list of snapshot dicts.
- `available_tools` — permission-compatible candidate tools.

`_select_research_tool` uses `select_preferred_tool`:

1. Default tool healthy or unknown → keep it (absence of evidence never
   changes planning).
2. Degraded/unavailable → pick the strictly healthier permission-compatible
   alternative, ranked by `(state, failure_rate, name)` — deterministic.
3. No strictly better alternative → **keep the requested tool**; execution,
   permissions, and retry semantics stay authoritative.

Unknown-health alternatives are never preferred over the requested tool.

### LLM planner

`LLMPlannerAgent` appends a bounded block (`build_planner_health_context`)
to the user message: at most 30 lines (alphabetical), one line per tool,
rounded rates, plus optional MCP server states. No raw history, errors, or
payloads. The deterministic fallback receives the same input data, so
health-aware selection survives fallback.

### Workflow wiring

`_build_planner_input` (in `langgraph_workflow.py`) builds the planner input
**side-effect free**: snapshots come from the process-wide tracker; candidate
alternatives from a lightweight built-ins-only registry (planning never
connects MCP servers); MCP server states from the last lifecycle manager.

## Security

- Health evidence is derived only from registered-tool executions;
  **permission denials, disabled-tool blocks, and unknown-tool lookups are
  policy outcomes and are never recorded** as availability evidence.
- Disabled tools remain disabled (`ToolDefinition.enabled`, fail-closed).
- Snapshots are frozen; planner code has no write path to health state.
- Snapshots/context contain no tenant, user, payload, or error data; the
  context is identical for all tenants.
- Health cannot override policy: it only reorders candidates already produced
  by the operator-controlled registry; execution re-validates everything.

## Metrics (bounded labels only)

- `tool_health_state_changes_total{from_state,to_state}` — transitions.
- `tool_health_states{state}` — gauge of tools per state.

No tool names, job IDs, errors, or URLs appear as labels. Alert added:
`WidespreadToolDegradation` (≥3 tools degraded/unavailable for 10m).
Deliberately **no** per-tool alerts: single-tool degradation is routine and
the planner already routes around it. No Grafana dashboard changes: the two
gauges/counter are visible through standard Prometheus queries and adding
panels to existing dashboards was judged clutter, not value.

## Limitations

- Health is **process-local** (in-memory): worker restarts reset evidence.
  By design — health reflects recent evidence, not durable facts.
- In-flight workflow plans are not re-planned on health change; a plan
  already carries its chosen tools.
- Deterministic selection considers built-in tools (MCP tool names are only
  known after server connection, and planning must stay side-effect free).
- Health says nothing about *latency* or *quality*, only availability.
- At-least-once semantics unchanged; health provides no execution guarantee.
