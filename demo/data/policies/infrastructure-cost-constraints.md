# Infrastructure and Cost Constraints (Acme Systems)

Owner: Infrastructure Engineering / Finance
Status: Approved — fiscal year 2026/27
Classification: Internal

## IC-1 On-premises capacity

Three on-premises datacenters are available (London, Dublin, Frankfurt) with a
combined committed capacity of 2.4 PB and 1,100 compute vCPU-equivalents. There
is no approved public-cloud region for regulated workloads in FY26/27.

## IC-2 Latency envelope

Regional distance between the London primary and the Dublin failover site
constrains cross-site replication latency. A solution requiring synchronous
cross-region writes across UK and EEA regions is not supported by the current
network fabric.

## IC-3 Staffing and support model

Platform Operations maintains a 24x7 follow-the-sun rota with six platform
engineers. A candidate requiring a dedicated vendor-managed operations team, or
requiring Acme staff to hold vendor-certified administrator credentials only,
introduces an operational risk that must be assessed explicitly.

## IC-4 Cost ceiling

Total run-rate cost for the data processing platform must not exceed
GBP 1,850,000 per year, including licence, infrastructure, and the 24x7 support
contract. Any three-year total-cost-of-ownership projection above GBP 5,000,000
must be escalated to the CFO before a pilot is approved.

## IC-5 Exit and lock-in

Annual exit cost — the cost of migrating away within twelve months — must remain
below GBP 300,000. Solutions that require rewriting business logic in a
proprietary query language score materially worse on exit cost.