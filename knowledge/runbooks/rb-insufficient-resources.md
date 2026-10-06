---
id: rb-insufficient-resources
title: Pods Pending with Insufficient cpu or memory
doc_type: runbook
categories: [INSUFFICIENT_RESOURCES]
services: [payments-api, orders-api, inventory-api, redis]
tags: [scheduling, pending, requests, capacity]
severity: medium
last_reviewed: 2026-07-18
related: [rb-scheduling-constraints, rb-oom-killed, k8s-manage-resources-containers]
---
# Pods Pending with Insufficient cpu or memory

## Symptoms
- Pods stay `Pending` with no node assigned (`NODE` column is `<none>`).
- Event `FailedScheduling`: `0/1 nodes are available: 1 Insufficient memory` or
  `Insufficient cpu`.
- A rollout or scale-up stalls; existing pods keep running if they were not replaced.

## Quick checks (read-only)
```bash
kubectl -n shop get pods -o wide | grep Pending
kubectl -n shop describe pod <pod> | sed -n '/Events/,$p'
kubectl get nodes
kubectl describe node <node> | sed -n '/Allocated resources/,/Events/p'
kubectl -n shop get deploy <deployment> -o jsonpath='{.spec.template.spec.containers[0].resources}'
kubectl -n shop get deploy -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas
```
Scheduling uses **requests**, not live usage. A node can look idle in `top` and still
reject a pod whose request does not fit the remaining allocatable capacity.
Shopfront app pods request 32Mi / 10m each.

## Diagnosis decision tree
1. Does the scheduling message say `Insufficient cpu` or `Insufficient memory`? If it
   mentions node selectors, affinity or taints instead, use the scheduling-constraints runbook.
2. Did a request change? A request raised from 32Mi to 2Gi in one revision will never
   fit the demo node. Compare with the previous revision.
3. Did replicas change? A manual scale to 10 replicas can exhaust the node.
4. Are other workloads consuming the node (`Allocated resources` near 100%)? Then this
   is cluster capacity, not the service.

## Remediation options
- Revert an oversized request (`patch_container_resources`) or roll back the revision
  (`rollback_deployment`).
- Scale back an accidental replica increase (`scale_deployment`).
- Genuine capacity shortfall: escalate to team-platform to add nodes.

## Do NOT
- Do not remove requests to squeeze pods in; it makes OOM kills and noisy-neighbour
  problems more likely.
- Do not delete other teams' pods to free capacity.

## Verify recovery
- Pod is scheduled (node assigned), Running, then Ready.
- No new `FailedScheduling` events.

## Escalation
team-platform for node capacity; the owning team for request changes.

## Related
- [rb-scheduling-constraints](rb-scheduling-constraints.md)
- [rb-oom-killed](rb-oom-killed.md)
- [k8s-manage-resources-containers](../k8s-docs/k8s-manage-resources-containers.md)
