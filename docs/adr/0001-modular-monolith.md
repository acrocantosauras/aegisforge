# ADR 0001: Prefer a Modular Monolith for the Initial Platform

- Status: Accepted
- Date: 2026-09-02

## Context

AegisForge is planned as an enterprise multi-agent AI platform. At Phase 0, the system is not yet served by enough workload or organizational complexity to justify a distributed service architecture. The team still needs strong separation between governance and execution, but the initial design should remain simple, fast to build, and easy to test.

## Decision

We will start with a modular monolith implemented in Python, with explicit package boundaries for domain, API, orchestration, agents, tools, knowledge, and infrastructure support. This structure will allow the platform to evolve into a service-oriented design later without redesigning the domain model or interfaces.

## Alternatives Considered

1. Full microservice architecture
   - Pros: independent scaling and deployment
   - Cons: operational overhead, slow startup, harder debugging, excessive complexity for early phase

2. Single script or notebook-like prototype
   - Pros: fastest path to demo output
   - Cons: unacceptable for security, auditability, and maintainability

3. Service-per-agent model from day one
   - Pros: clear boundaries for future growth
   - Cons: unnecessary complexity and not justified by current requirements

## Trade-offs

- Benefits: low deployment overhead, easier local development, clearer code ownership, simpler testing
- Costs: future scaling may require reorganizing modules or extracting services once workload and concurrency demands justify it

## Consequences

The modular monolith will enforce clear boundaries and typed contracts, while still allowing extraction to separate services when the product needs it. This keeps the initial architecture honest and production-minded without overbuilding.

## Deferred Decisions

- distributed orchestration and worker queue are deferred until workload and downtime requirements justify them
- separate service deployment for RAG, tool execution, and evaluation is deferred
- dedicated external Redis and PostgreSQL services are introduced only when real operational needs appear
