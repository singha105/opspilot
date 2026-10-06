---
id: rb-ingress-502
title: Ingress returns 502 or 503 to external clients
doc_type: runbook
categories: []
services: [orders-api, payments-api]
tags: [ingress, http-502, gateway, load-balancer, edge]
severity: high
last_reviewed: 2026-06-12
related: [rb-service-misconfig, rb-readiness-probe-failures, rb-tls-certificate-expiry, pm-2026-07-checkout-ingress-502]
---
# Ingress returns 502 or 503 to external clients

## Symptoms
- Customers or synthetic checks get `502 Bad Gateway` or `503 Service Temporarily
  Unavailable` from the storefront edge, while in-cluster calls between services work.
- Ingress controller logs show `upstream prematurely closed connection`,
  `connect() failed (111: Connection refused) while connecting to upstream` or
  `no live upstreams`.
- Alert `IngressHighErrorRate` from the edge dashboards.

## Quick checks (read-only)
```bash
kubectl -n shop get ingress
kubectl -n shop describe ingress <name>
kubectl -n ingress-nginx logs deploy/ingress-nginx-controller --tail=100 | grep -E ' 50[234] '
kubectl -n shop get endpointslices -l kubernetes.io/service-name=<backend-service>
kubectl -n shop get pods -l app.kubernetes.io/name=<backend-service>
```
In production, the storefront Ingress routes `/api/orders` to `orders-api:8080`. The
demo cluster has no ingress controller (traefik is disabled).

## Diagnosis decision tree
1. 503 with `no live upstreams`: the backend Service has no Ready endpoints. Follow the
   readiness or service-misconfig runbook for the backend.
2. 502 with `connection refused`: endpoints exist but nothing listens on the target port.
   Check the Ingress `backend.service.port` against the Service port.
3. 502 with `upstream prematurely closed connection`: the backend closes keep-alive
   connections before the proxy expects it. Compare the backend's keep-alive timeout
   with the controller's `upstream-keepalive-timeout`; the backend must be longer.
4. Errors only during deploys? Pods are removed from endpoints after the proxy already
   chose them; add a `preStop` delay and graceful shutdown.
5. Only HTTPS failing? See the TLS certificate runbook.

## Remediation options
- Fix the backend first if it has no endpoints.
- Align keep-alive timeouts or the Ingress backend port through team-platform.

## Do NOT
- Do not raise proxy timeouts to hide a slow backend.
- Do not bypass the Ingress with a NodePort for customer traffic.

## Verify recovery
- Edge error rate back under 0.1%; controller logs stop showing upstream errors.

## Escalation
team-platform owns the ingress controller; owning team for the backend.

## Related
- [rb-service-misconfig](rb-service-misconfig.md)
- [rb-readiness-probe-failures](rb-readiness-probe-failures.md)
- [rb-tls-certificate-expiry](rb-tls-certificate-expiry.md)
- [pm-2026-07-checkout-ingress-502](../postmortems/pm-2026-07-checkout-ingress-502.md)
