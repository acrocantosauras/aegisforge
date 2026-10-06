# Current Data Platform Architecture (Acme Systems)

Owner: Platform Engineering
Status: As-built — 2026
Classification: Internal

## Estate overview

The current data processing platform is a self-managed deployment of Apache
Kafka 3.6 (London and Dublin) feeding a bespoke stream-processing service,
with PostgreSQL 15 as the operational store and an internal schema registry.

## Known characteristics

End-to-end ingest latency at p99 is currently 310 ms under production peak,
which exceeds the 250 ms requirement in AR-2. Sustained throughput is
approximately 70,000 events per second, below the 120,000 events per second
requirement. Back-pressure is currently handled by an unmanaged Kafka retention
policy, which has twice caused silent consumer lag.

## Storage footprint

Committed capacity in use is 1.35 PB of the 2.4 PB available across the three
datacenters. All regulated data is stored in the UK and EEA only.

## Operational history

The bespoke stream-processing service has a mean time to recovery of 92 minutes
and requires two platform engineers with vendor-certified administrator
credentials to perform schema migrations. It has no documented dry-run mode for
pipeline changes, which violates AR-4.

## Binding constraints on replacement

The Kafka event bus, the PostgreSQL operational store, and the schema registry
are shared with 14 other production services. A replacement platform must coexist
with them; a rip-and-replace of the shared bus is not in scope.