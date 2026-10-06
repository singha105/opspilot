---
id: pm-2025-11-orders-bad-image-tag
title: "Postmortem: orders-api release pointed at an image tag that was never built"
doc_type: postmortem
categories: [IMAGE_PULL_ERROR]
services: [orders-api]
tags: [postmortem, image-tag, release, imagepullbackoff]
date: 2025-11-26
severity: SEV-2
related: [rb-image-pull-error, rb-bad-rollout, svc-orders-api]
---
# Postmortem: orders-api release pointed at an image tag that was never built

## Summary
On 26 November 2025 an orders-api release referenced the tag `2025.11.26-rc1`, but the
CI job that builds and pushes images had failed earlier that day. The new pod stayed in
`ErrImagePull` and then `ImagePullBackOff`. Orders were unavailable for 11 minutes
during the busiest week of the year.

## Impact
- Order creation failed for 11 minutes (19:31–19:42 UTC); product browsing worked.
- About 900 abandoned carts; most customers retried successfully afterwards.

## Timeline (UTC)
- 19:12 Image build job failed on a flaky dependency download. Nobody noticed.
- 19:30 Release pipeline updated the Deployment image to `orders-api:2025.11.26-rc1`.
- 19:31 Old pod terminated (single replica, no surge). New pod: `ErrImagePull`.
- 19:32 Event: `Failed to pull image ... manifest unknown`.
- 19:34 Alert `KubeDeploymentReplicasMismatch` for orders-api.
- 19:39 On-call read the event, confirmed the tag did not exist in the registry.
- 19:41 `kubectl rollout undo deploy/orders-api`.
- 19:42 Previous image running, pod Ready.

## Root cause
The release pipeline did not verify that the image tag existed before updating the
Deployment. The pull error message (`manifest unknown`) was precise, and the fix was a
rollback to the previous tag.

## Contributing factors
- Single replica with `maxSurge: 0` turned a bad tag into a full outage.
- Build failures were reported in a channel nobody watched during the evening.

## What went well
- Clear event messages; diagnosis took under five minutes once someone looked.

## Action items
- Pipeline checks the registry for the tag before deploying. Owner: team-orders. Done.
- Pin images by digest in release manifests. Owner: team-platform.
- Route build failures to the release channel. Owner: team-orders. Done.

## Related
- [rb-image-pull-error](../runbooks/rb-image-pull-error.md)
- [rb-bad-rollout](../runbooks/rb-bad-rollout.md)
- [svc-orders-api](../services/svc-orders-api.md)
