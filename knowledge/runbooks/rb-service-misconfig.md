---
id: rb-service-misconfig
title: Service has no endpoints or routes to the wrong port
doc_type: runbook
categories: [SERVICE_MISCONFIG]
services: [payments-api, orders-api, inventory-api, redis]
tags: [service, selector, endpoints, targetport]
severity: high
last_reviewed: 2026-08-22
related: [rb-dependency-unavailable, rb-readiness-probe-failures, rb-ingress-502, pm-2026-08-inventory-service-selector, k8s-debug-service]
---
# Service has no endpoints or routes to the wrong port

## Symptoms
- The backing pods are `1/1` Ready and healthy, yet callers get `Connection refused`
  or time out when using the Service name.
- `kubectl get endpointslices` for the Service is empty, or lists the wrong pods.
- Problems start right after a change to labels, the Service selector, or port names.

## Quick checks (read-only)
```bash
kubectl -n shop get svc <service> -o yaml
kubectl -n shop get endpointslices -l kubernetes.io/service-name=<service> -o wide
kubectl -n shop get pods --show-labels
kubectl -n shop get pods -l app.kubernetes.io/name=<service> -o wide
kubectl -n shop get deploy <service> -o jsonpath='{.spec.template.spec.containers[0].ports}'
```
Shopfront Services select on `app.kubernetes.io/name: <service>` and map port 8080 to
the container port named `http` (redis: 6379 to `redis`).

## Diagnosis decision tree
1. Are the pods Ready? If not, this is a readiness problem, not a Service problem: use
   the readiness runbook.
2. Pods Ready but EndpointSlice empty? The selector matches nothing. Compare
   `spec.selector` with the pod labels, character for character.
3. EndpointSlice lists pods but traffic still fails? Check `targetPort`. A number that
   does not match the container port, or a port **name** that the container does not
   define, routes traffic nowhere.
4. Endpoints correct and ports correct? Test name resolution; see the DNS runbook.

## Remediation options
- Restore the Service selector or ports from the base manifests (owning team change).
- If a Deployment rollout changed pod labels or port names, roll it back
  (`rollback_deployment`).

## Do NOT
- Do not create a second Service with a different name as a workaround; every caller
  is configured with the existing name (`PAYMENTS_URL`, `REDIS_URL`).
- Do not change pod labels by hand on running pods; the Deployment will drift.

## Verify recovery
- The EndpointSlice lists the expected pod IPs and port.
- Callers log successful calls and return to Ready.

## Escalation
Owning team of the Service.

## Related
- [rb-dependency-unavailable](rb-dependency-unavailable.md)
- [rb-readiness-probe-failures](rb-readiness-probe-failures.md)
- [rb-ingress-502](rb-ingress-502.md)
- [pm-2026-08-inventory-service-selector](../postmortems/pm-2026-08-inventory-service-selector.md)
- [k8s-debug-service](../k8s-docs/k8s-debug-service.md)
