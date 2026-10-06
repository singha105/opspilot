---
id: rb-dns-resolution-failures
title: In-cluster DNS lookups failing or slow
doc_type: runbook
categories: []
services: [payments-api, orders-api, inventory-api, redis]
tags: [dns, coredns, resolution, networking]
severity: high
last_reviewed: 2026-06-30
related: [rb-dependency-unavailable, pm-2025-12-coredns-overload, k8s-dns-debugging-resolution]
---
# In-cluster DNS lookups failing or slow

## Symptoms
- Application errors mention name resolution rather than connections:
  `socket.gaierror: [Errno -2] Name or service not known`,
  `Temporary failure in name resolution`, `no such host`, or lookups taking 5 seconds
  (the default resolver timeout) before succeeding.
- Many services across namespaces are affected at once, not one dependency.
- CoreDNS pods in `kube-system` show restarts, high CPU, or `SERVFAIL` in their logs.

## Quick checks (read-only)
```bash
kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=50
kubectl -n kube-system get svc kube-dns
kubectl -n kube-system get endpointslices -l kubernetes.io/service-name=kube-dns
kubectl -n shop get pod <pod> -o jsonpath='{.spec.dnsPolicy}'
kubectl -n kube-system get configmap coredns -o yaml
```

## Diagnosis decision tree
1. Is the error a resolution error or a connection error? `Connection refused` after a
   successful lookup is not DNS: go to the dependency or service runbooks.
2. Are CoreDNS pods Running and listed as kube-dns endpoints? No endpoints means no
   cluster DNS at all.
3. Do only external names fail (registry, payment provider) while `redis.shop` works?
   Then look at the upstream forwarders in the CoreDNS ConfigMap.
4. Are lookups slow rather than failing? Look for CoreDNS CPU throttling and the
   `ndots:5` search-path amplification on names without a trailing dot.
5. Did someone change the `coredns` ConfigMap recently?

## Remediation options
- Escalate to team-platform; CoreDNS is cluster infrastructure and not changed by
  service on-call.
- Workloads can temporarily use fully qualified names (`redis.shop.svc.cluster.local.`)
  to cut search-path lookups if latency is the issue.

## Do NOT
- Do not hard-code pod IPs in service config; they change on every restart.
- Do not edit the CoreDNS ConfigMap during an incident without team-platform.

## Verify recovery
- CoreDNS pods Ready; application logs stop showing resolution errors.

## Escalation
team-platform, high urgency: DNS failures affect every service.

## Related
- [rb-dependency-unavailable](rb-dependency-unavailable.md)
- [pm-2025-12-coredns-overload](../postmortems/pm-2025-12-coredns-overload.md)
- [k8s-dns-debugging-resolution](../k8s-docs/k8s-dns-debugging-resolution.md)
