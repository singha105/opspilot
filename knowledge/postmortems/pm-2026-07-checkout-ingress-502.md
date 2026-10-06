---
id: pm-2026-07-checkout-ingress-502
title: "Postmortem: intermittent 502s at the storefront edge from keep-alive mismatch"
doc_type: postmortem
categories: []
services: [orders-api]
tags: [postmortem, ingress, http-502, keep-alive, edge]
date: 2026-07-15
severity: SEV-3
related: [rb-ingress-502, rb-service-misconfig]
---
# Postmortem: intermittent 502s at the storefront edge from keep-alive mismatch

## Summary
For three days in July 2026 about 0.4% of storefront API requests returned
`502 Bad Gateway` from the ingress controller. Inside the cluster every service was
Ready and in-cluster calls succeeded. The ingress reused idle upstream connections that
orders-api had already closed: the backend's keep-alive timeout (5s) was shorter than
the controller's upstream keep-alive timeout (60s).

## Impact
- 0.4% of `/api/orders` requests failed with 502 for about 70 hours; customers saw
  occasional "something went wrong" messages and retried.

## Timeline (UTC)
- 07-12 22:00 orders-api web server upgraded; its default keep-alive dropped from 75s to 5s.
- 07-13 Error budget alert on the edge; pods healthy, so the alert was tuned as noise.
- 07-15 09:30 Controller logs showed `upstream prematurely closed connection while
  reading response header from upstream` for orders-api only.
- 07-15 11:00 Correlated with the server upgrade and its new default.
- 07-15 12:10 Backend keep-alive set to 75s; 502s stopped.

## Root cause
Edge connection handling, not a Kubernetes object misconfiguration: the backend closed
idle keep-alive connections before the proxy expected it. Endpoints, Service selector
and ports were all correct throughout.

## Contributing factors
- Only edge metrics showed the problem; service dashboards looked perfect.

## What went well
- Controller logs contained the exact upstream error.

## Action items
- Backend keep-alive must exceed the ingress upstream keep-alive; add to the service
  template. Owner: team-platform.
- Alert on edge 5xx by backend. Owner: team-platform.

## Related
- [rb-ingress-502](../runbooks/rb-ingress-502.md)
- [rb-service-misconfig](../runbooks/rb-service-misconfig.md)
