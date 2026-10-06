---
id: rb-disk-pressure-evictions
title: Pods evicted because of node disk or memory pressure
doc_type: runbook
categories: []
services: [payments-api, orders-api, inventory-api, redis]
tags: [eviction, disk-pressure, ephemeral-storage, node]
severity: high
last_reviewed: 2026-06-30
related: [rb-node-not-ready, rb-oom-killed]
---
# Pods evicted because of node disk or memory pressure

## Symptoms
- Pods with status `Evicted` (phase `Failed`) and messages such as
  `The node was low on resource: ephemeral-storage` or `... memory`.
- Node condition `DiskPressure=True` or `MemoryPressure=True`; taint
  `node.kubernetes.io/disk-pressure:NoSchedule`.
- New pods cannot be scheduled on the node; old evicted pod objects pile up.
- Unlike an OOM kill, evictions show no `OOMKilled` reason or exit code 137 in the
  container's last state; the whole pod is removed.

## Quick checks (read-only)
```bash
kubectl get pods -A --field-selector status.phase=Failed
kubectl -n shop describe pod <evicted-pod> | grep -E 'Status|Reason|Message'
kubectl describe node <node> | sed -n '/Conditions/,/Addresses/p'
kubectl get events -A --field-selector reason=Evicted --sort-by=.lastTimestamp
kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary | head -50
```

## Diagnosis decision tree
1. Which resource triggered eviction: `ephemeral-storage`, `memory`, `nodefs` or
   `imagefs`? The eviction message names it.
2. Disk: is one pod writing large logs or files to its writable layer or an `emptyDir`
   without a `sizeLimit`? Container logs at DEBUG level are a frequent cause.
3. Disk: are unused images filling `imagefs`? The kubelet garbage-collects them above
   85% usage by default.
4. Memory: pods without limits, or many pods whose requests understate real usage, push
   the node into memory pressure.

## Remediation options
- Lower an accidentally verbose `LOG_LEVEL` (Shopfront services accept DEBUG, INFO,
  WARNING, ERROR) through the owning team.
- Ask team-platform to clean images or expand the node disk.
- Add `ephemeral-storage` requests and limits to noisy workloads.

## Do NOT
- Do not delete evicted pods as the fix; they are evidence. Clean them up after the
  cause is addressed.

## Verify recovery
- Node conditions back to `False`, taint removed, pods scheduled again.

## Escalation
team-platform for nodes; owning team for the workload that filled the disk.

## Related
- [rb-node-not-ready](rb-node-not-ready.md)
- [rb-oom-killed](rb-oom-killed.md)
