---
id: rb-hpa-flapping
title: HorizontalPodAutoscaler scaling up and down repeatedly
doc_type: runbook
categories: []
services: [orders-api, payments-api]
tags: [hpa, autoscaling, metrics, replicas]
severity: low
last_reviewed: 2026-05-28
related: [rb-insufficient-resources]
---
# HorizontalPodAutoscaler scaling up and down repeatedly

## Symptoms
- Replica count oscillates every few minutes (for example 2 → 6 → 2) without a matching
  change in traffic.
- Events `SuccessfulRescale` with alternating `New size` values.
- Latency spikes each time pods are removed; cold pods fail readiness briefly.
- HPA status shows `<unknown>` metrics or `FailedGetResourceMetric`.

## Quick checks (read-only)
```bash
kubectl -n shop get hpa
kubectl -n shop describe hpa <name>
kubectl -n shop get events --field-selector reason=SuccessfulRescale --sort-by=.lastTimestamp
kubectl -n kube-system get pods -l k8s-app=metrics-server
kubectl -n shop get deploy <deployment> -o jsonpath='{.spec.template.spec.containers[0].resources.requests}'
```
Note: the demo cluster runs without metrics-server, so HPAs only exist in the
production Shopfront environment.

## Diagnosis decision tree
1. Are metrics available? `<unknown>` targets mean the HPA cannot read metrics and may
   hold or jump replica counts. Check metrics-server health.
2. Is the CPU request tiny? Utilization is measured against requests: a 10m request
   makes ordinary background work look like 300% utilization.
3. Is the scale-down stabilization window too short (`behavior.scaleDown`)? The
   default 300 seconds prevents most flapping; a value near 0 causes it.
4. Does the workload's CPU itself oscillate (batch work every few minutes)? Then the
   HPA is reacting correctly and the target needs tuning.
5. Do new pods spike CPU while starting (imports, cache warm-up)? Each scale-up then
   raises average utilization, which triggers another scale-up; a startup probe and
   `behavior.scaleUp` limits break that loop.
6. Is something else changing `spec.replicas` (a deploy pipeline applying a fixed
   replica count)? The pipeline and the HPA then fight each other.

## Remediation options
- Increase the scale-down stabilization window or set a sensible `minReplicas`
  (owning team change).
- Right-size CPU requests so utilization percentages mean something.

## Do NOT
- Do not delete the HPA during peak traffic; replicas freeze at the current count.
- Do not manually scale a Deployment that an HPA controls; the HPA overrides it.

## Verify recovery
- Replica count stable for 30 minutes; no alternating rescale events.

## Escalation
Owning team; team-platform if metrics-server is unhealthy.

## Related
- [rb-insufficient-resources](rb-insufficient-resources.md)
