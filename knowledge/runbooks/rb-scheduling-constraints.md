---
id: rb-scheduling-constraints
title: Pods unschedulable because of node selectors, affinity or taints
doc_type: runbook
categories: [SCHEDULING_CONSTRAINT]
services: [payments-api, orders-api, inventory-api, redis]
tags: [scheduling, pending, affinity, taints, node-selector]
severity: medium
last_reviewed: 2026-07-18
related: [rb-insufficient-resources, rb-node-not-ready, k8s-assign-pod-node, k8s-taint-and-toleration]
---
# Pods unschedulable because of node selectors, affinity or taints

## Symptoms
- Pods remain `Pending`; no node is assigned.
- `FailedScheduling` events mention placement rules rather than capacity, for example
  `0/1 nodes are available: 1 node(s) didn't match Pod's node affinity/selector` or
  `1 node(s) had untolerated taint {dedicated: batch}`.
- Often appears right after a manifest change that added `nodeSelector`, `affinity`,
  `topologySpreadConstraints` or after someone tainted a node.

## Quick checks (read-only)
```bash
kubectl -n shop describe pod <pod> | sed -n '/Events/,$p'
kubectl -n shop get pod <pod> -o jsonpath='{.spec.nodeSelector}{"\n"}{.spec.affinity}{"\n"}{.spec.tolerations}'
kubectl get nodes --show-labels
kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints
kubectl -n shop rollout history deploy/<deployment>
```

## Diagnosis decision tree
1. Read the `FailedScheduling` message. Each clause counts nodes rejected per reason.
   - `didn't match Pod's node affinity/selector`: the pod asks for a label no node has.
     Compare `nodeSelector` keys and values with `kubectl get nodes --show-labels`;
     typos such as `disktype: ssd` vs `disk-type: ssd` are common.
   - `had untolerated taint`: a node was tainted (maintenance, dedicated pool) and the
     pod has no matching toleration.
   - `didn't match pod anti-affinity rules` or `topology spread constraints`: with one
     node and `requiredDuringScheduling` anti-affinity, a second replica can never fit.
   - `Insufficient cpu/memory`: this is a capacity problem; use the insufficient
     resources runbook.
2. Did the rule arrive with the latest revision, or did the node change (new taint,
   relabel)? The first points at the service owner, the second at team-platform.

## Remediation options
- Roll back the revision that added the rule (`rollback_deployment`).
- If the node was tainted for maintenance on purpose, wait for the maintenance window
  to end rather than adding tolerations.
- Fix a label typo in the manifest through the owning team's normal change process.

## Do NOT
- Do not remove taints from nodes you do not own.
- Do not add a blanket toleration (`operator: Exists`); it lets the pod land on nodes
  reserved for other workloads.

## Verify recovery
- Pod is scheduled and becomes Ready; no further `FailedScheduling` events.

## Escalation
team-platform for node labels and taints; the owning team for affinity rules.

## Related
- [rb-insufficient-resources](rb-insufficient-resources.md)
- [rb-node-not-ready](rb-node-not-ready.md)
- [k8s-assign-pod-node](../k8s-docs/k8s-assign-pod-node.md)
- [k8s-taint-and-toleration](../k8s-docs/k8s-taint-and-toleration.md)
