# Engineering Standards

## Python and Tooling

- Python 3.12+
- dependency management via pip with a project metadata file and optional dev extras
- formatters: Ruff
- type checking: mypy
- test runner: pytest

## Repository Standards

- keep domain contracts stable and explicit
- prefer typed interfaces over ad hoc dictionaries for cross-module boundaries
- deny-by-default for tools and permissions
- emit structured logs and audit events for all significant actions
- keep secrets out of source control and out of default configuration

## Environment and Configuration

- environment variables for configuration and secrets
- use .env.example files for configuration examples only
- never commit production secrets or credentials

## API and Data Standards

- use API versioning for future HTTP interfaces
- use explicit schemas for request/response payloads
- plan for database migrations once persistence is introduced
- prefer deterministic runtime logic over hidden side effects

## Operational Standards

- structured logging with request and session correlation IDs
- explicit exception handling with non-sensitive error payloads
- monitor latency, failure, and retry rates as soon as runtime components exist

## Git and Documentation

- keep commits small and focused
- document architectural decisions in ADRs
- update README and docs when project boundaries change
