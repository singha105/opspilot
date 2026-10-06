---
id: rb-node-not-ready
title: Node NotReady and workloads being rescheduled
doc_type: runbook
categories: []
services: [payments-api, orders-api, inventory-api, redis]
tags: [node, kubelet, notready, infrastructure]
severity: critical
last_reviewed: 2026-06-30
related: [rb-disk-pressure-evictions, rb-scheduling-constraints]
---
# Node NotReady and workloads being rescheduled

## Symptoms
- `kubectl get nodes` shows a node `NotReady` or `Unknown`.
- Pods on that node show `Terminating` or `Unknown` and are recreated elsewhere after
  the default 5-minute toleration for `node.kubernetes.io/unreachable`.
- Alerts `KubeNodeNotReady` and `KubeNodeUnreachable`; many unrelated services degrade
  at the same time.

## Quick checks (read-only)
```bash
kubectl get nodes -o wide
kubectl describe node <node> | sed -n '/Conditions/,/Addresses/p'
kubectl get pods -A -o wide --field-selector spec.nodeName=<node>
kubectl get events -A --field-selector involvedObject.kind=Node --sort-by=.lastTimestamp
kubectl get lease -n kube-node-lease <node> -o jsonpath='{.spec.renewTime}'
```

## Diagnosis decision tree
1. One node or all nodes? All nodes NotReady points at the control plane or the network,
   not individual machines.
2. What do the node conditions say? `Ready=Unknown` with
   `Kubelet stopped posting node status` means the kubelet or the machine is down.
   `MemoryPressure`, `DiskPressure` or `PIDPressure` true means the kubelet is alive
   but the node is unhealthy; see the evictions runbook.
3. Is the node lease still renewing? A stale lease confirms the kubelet is not reaching
   the API server.
4. Was there planned maintenance (cordon, drain) or a cloud provider event?
5. Did the container runtime stop? `kubectl describe node` may show
   `container runtime is down` or `PLEG is not healthy`; the kubelet is alive but cannot
   manage containers, so pods on the node stop being updated.
6. On the single-node demo cluster (k3d), a NotReady node usually means the Docker
   runtime on the laptop was paused or ran out of memory; check the host first.

## Remediation options
- Infrastructure owns node recovery: reboot or replace the node.
- Service on-call checks that their workloads rescheduled; single-replica services
  (redis, all demo services) are down until rescheduling completes.

## Do NOT
- Do not force-delete pods on an unreachable node unless team-platform confirms the
  node is truly gone; two copies of a stateful pod can corrupt data.

## Verify recovery
- Node `Ready`, or workloads Running on other nodes; alerts resolved.

## Escalation
team-platform immediately; this is an infrastructure incident.

## Related
- [rb-disk-pressure-evictions](rb-disk-pressure-evictions.md)
- [rb-scheduling-constraints](rb-scheduling-constraints.md)
