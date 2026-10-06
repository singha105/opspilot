---
id: pm-2026-05-orders-liveness-timeout
title: "Postmortem: orders-api restart storm from an aggressive liveness timeout"
doc_type: postmortem
categories: [LIVENESS_PROBE_MISCONFIG]
services: [orders-api]
tags: [postmortem, liveness, probes, restarts, cpu-throttling]
date: 2026-05-08
severity: SEV-2
related: [rb-liveness-probe-failures, svc-orders-api]
---
# Postmortem: orders-api restart storm from an aggressive liveness timeout

## Summary
On 8 May 2026 orders-api restarted 70 times in two hours. A tuning change had lowered
the liveness probe `timeoutSeconds` from 3 to 1 and `failureThreshold` from 3 to 1.
During a marketing push the container hit its 200m CPU limit, `/healthz` took longer
than one second under throttling, and the kubelet killed healthy containers. Each
restart lost in-flight orders.

## Impact
- 3.4% of order requests failed over two hours (11:00–13:05 UTC).

## Timeline (UTC)
- 09:15 Probe tuning deployed ("faster detection of hung pods").
- 11:00 Traffic rises; CPU throttling on orders-api above 60%.
- 11:02 Events: `Liveness probe failed: Get "http://10.42.0.17:8080/healthz": context deadline exceeded`,
  then `Container app failed liveness probe, will be restarted`.
- 11:20 Restart count climbing; last state `Error`, exit code 137 caused by the kill, not OOM.
- 12:10 On-call ruled out OOM (no `OOMKilled` reason) and memory growth.
- 12:50 Probe timing change found in revision history.
- 13:05 Probe restored to 3s timeout and threshold 3; restarts stopped.

## Root cause
A liveness probe misconfiguration: a timeout too short for the service under CPU
throttling turned slow responses into restarts. The application never hung.

## Contributing factors
- Liveness failure threshold of 1 means a single slow response kills the container.
- CPU limit sized for average, not peak, traffic.

## What went well
- Exit code and the absence of `OOMKilled` quickly excluded memory problems.

## Action items
- Probe policy: liveness timeout at least 2s, failure threshold at least 3. Owner: team-platform.
- Review CPU limits for peak load. Owner: team-orders.

## Related
- [rb-liveness-probe-failures](../runbooks/rb-liveness-probe-failures.md)
- [svc-orders-api](../services/svc-orders-api.md)
