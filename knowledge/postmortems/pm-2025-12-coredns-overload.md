---
id: pm-2025-12-coredns-overload
title: "Postmortem: slow lookups and resolution failures from overloaded CoreDNS"
doc_type: postmortem
categories: []
services: [orders-api, payments-api, inventory-api]
tags: [postmortem, dns, coredns, ndots, latency]
date: 2025-12-03
severity: SEV-2
related: [rb-dns-resolution-failures, rb-dependency-unavailable]
---
# Postmortem: slow lookups and resolution failures from overloaded CoreDNS

## Summary
On 3 December 2025 latency across all Shopfront services rose sharply and some calls
failed with `Temporary failure in name resolution`. A new client library resolved the
payment provider's hostname on every request without caching. With the default
`ndots:5` search path, each lookup became up to five queries, and the two CoreDNS pods
saturated their 100m CPU limit.

## Impact
- p95 latency of checkout rose from 280 ms to 4.9 s for 45 minutes; 2% of requests failed.

## Timeline (UTC)
- 14:00 Client library upgrade deployed to orders-api and payments-api.
- 14:20 Latency alerts for several services at once.
- 14:30 Logs: `socket.gaierror: [Errno -3] Temporary failure in name resolution`.
- 14:40 CoreDNS pods throttled; logs show bursts of `NXDOMAIN` for search-path variants
  such as `api.provider.example.shop.svc.cluster.local`.
- 14:55 team-platform scaled CoreDNS to four replicas and raised its CPU limit.
- 15:05 Latency back to normal.

## Root cause
Cluster DNS capacity, triggered by uncached lookups amplified by the search path. No
single service dependency was down; connection errors only followed failed lookups.

## Contributing factors
- No DNS cache (NodeLocal DNSCache) in the cluster.

## What went well
- Error messages clearly said "name resolution", which pointed away from the services.

## Action items
- Use fully qualified names with a trailing dot for external hosts. Owner: service teams.
- Deploy NodeLocal DNSCache. Owner: team-platform.

## Related
- [rb-dns-resolution-failures](../runbooks/rb-dns-resolution-failures.md)
- [rb-dependency-unavailable](../runbooks/rb-dependency-unavailable.md)
