---
id: pm-2026-08-inventory-service-selector
title: "Postmortem: inventory-api Service lost its endpoints after a label rename"
doc_type: postmortem
categories: [SERVICE_MISCONFIG]
services: [inventory-api]
tags: [postmortem, service, selector, labels, endpoints]
date: 2026-08-04
severity: SEV-2
related: [rb-service-misconfig, svc-inventory-api]
---
# Postmortem: inventory-api Service lost its endpoints after a label rename

## Summary
On 4 August 2026 a labelling cleanup changed the inventory-api pod template label from
`app.kubernetes.io/name: inventory-api` to `app.kubernetes.io/name: inventory`. The
Service selector still used the old value. Pods were Running and Ready, but the
Service's EndpointSlice became empty, so every caller got `Connection refused` from
`inventory-api:8080`.

## Impact
- Stock lookups failed for 17 minutes (13:12–13:29 UTC).

## Timeline (UTC)
- 13:11 Label cleanup deployed for inventory-api's Deployment only.
- 13:12 New pod Ready; old pod removed. EndpointSlice for `inventory-api` empty.
- 13:14 Product page errors; synthetic check fails with connection refused.
- 13:18 On-call saw the pod Ready with no restarts and suspected networking.
- 13:24 `kubectl get endpointslices -l kubernetes.io/service-name=inventory-api` empty;
  selector compared with pod labels.
- 13:27 Deployment rolled back to the previous labels.
- 13:29 Endpoints restored.

## Root cause
Service misconfiguration: the selector no longer matched the pods' labels. Readiness
was fine; the Service simply selected nothing.

## Contributing factors
- Service and Deployment live in the same file but the cleanup script edited only
  Deployments.

## What went well
- Rollback took two minutes once the empty EndpointSlice was found.

## Action items
- CI check: every Service selector matches at least one pod template in the overlay.
  Owner: team-platform.
- Runbook: check EndpointSlices first when Ready pods are unreachable. Done.

## Related
- [rb-service-misconfig](../runbooks/rb-service-misconfig.md)
- [svc-inventory-api](../services/svc-inventory-api.md)
