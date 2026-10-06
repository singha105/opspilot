---
id: rb-apiserver-latency
title: Kubernetes API server slow or timing out
doc_type: runbook
categories: []
services: []
tags: [apiserver, control-plane, latency, etcd]
severity: high
last_reviewed: 2026-05-20
related: [rb-node-not-ready]
---
# Kubernetes API server slow or timing out

## Symptoms
- `kubectl` commands take many seconds or fail with
  `Error from server (Timeout): the server was unable to return a response in the time allotted`
  or `net/http: TLS handshake timeout`.
- Controllers fall behind: Deployments take minutes to create ReplicaSets, endpoint
  updates lag, HPAs stop reacting.
- Alerts `KubeAPIErrorBudgetBurn` or `KubeAPILatencyHigh`; `429 Too Many Requests`
  responses from priority and fairness throttling.
- Running application pods keep serving traffic; only control-plane operations suffer.

## Quick checks (read-only)
```bash
kubectl get --raw='/readyz?verbose' | tail -20
kubectl get --raw='/livez?verbose' | tail -5
kubectl get --raw /metrics | grep -E '^apiserver_request_duration_seconds_count' | head
kubectl get flowschemas,prioritylevelconfigurations
kubectl get events -A --sort-by=.lastTimestamp | tail -20
```

## Diagnosis decision tree
1. Is it every request or only some resources? Slow `list` calls on large resources
   (events, secrets across all namespaces) usually come from one heavy client.
2. Are requests being throttled (429)? Check which FlowSchema the client lands in;
   a misbehaving controller or a script polling `kubectl get pods -A` in a loop is common.
3. Does `/readyz?verbose` show `etcd` failing? etcd latency (disk, defragmentation)
   makes every write slow.
4. Did a new operator or CRD controller get installed recently?
5. Are admission webhooks slow? A validating or mutating webhook whose backing service
   is down adds its full timeout to every matching request. Look for
   `failed calling webhook` in events and check `kubectl get validatingwebhookconfigurations`.
6. Is the cluster large enough that unpaginated lists are expensive? Tools that list
   every pod in every namespace once a second scale badly.

## Remediation options
- Identify and stop the abusive client (team-platform).
- Reduce watch and list pressure from in-house tooling: use informers and label
  selectors instead of full lists.

## Do NOT
- Do not restart the API server or etcd during an incident without the platform team.
- Do not start more clients "to see what is happening"; that adds load.

## Verify recovery
- `kubectl` latency back under one second; controllers catch up; alerts resolve.

## Escalation
team-platform, high urgency.

## Related
- [rb-node-not-ready](rb-node-not-ready.md)
