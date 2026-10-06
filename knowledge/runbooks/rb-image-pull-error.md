---
id: rb-image-pull-error
title: ErrImagePull and ImagePullBackOff
doc_type: runbook
categories: [IMAGE_PULL_ERROR]
services: [payments-api, orders-api, inventory-api, redis]
tags: [image, registry, rollout, tag]
severity: high
last_reviewed: 2026-08-14
related: [rb-bad-rollout, pm-2025-11-orders-bad-image-tag, k8s-images]
---
# ErrImagePull and ImagePullBackOff

## Symptoms
- New pods stay in `ContainerCreating`, then `ErrImagePull`, then `ImagePullBackOff`.
- `kubectl get deploy` shows fewer available replicas than desired after a rollout.
- Events contain `Failed to pull image`, `manifest unknown`, `not found`,
  `pull access denied` or `unauthorized`.
- Because Shopfront Deployments roll with `maxSurge: 0`, the old pod is removed first,
  so a bad image means the service is down, not just degraded.

## Quick checks (read-only)
```bash
kubectl -n shop get pods -o wide
kubectl -n shop describe pod <pod> | sed -n '/Events/,$p'
kubectl -n shop get deploy <deployment> -o jsonpath='{.spec.template.spec.containers[*].image}'
kubectl -n shop get deploy <deployment> -o jsonpath='{.spec.template.spec.containers[*].imagePullPolicy}'
kubectl -n shop rollout history deploy/<deployment>
kubectl -n shop get events --field-selector reason=Failed --sort-by=.lastTimestamp
```
Shopfront app images are `opspilot-demo-svc:<tag>` and are imported into the cluster,
so `imagePullPolicy: IfNotPresent` normally finds them locally. A tag that was never
imported makes the kubelet fall back to Docker Hub, which fails.

## Diagnosis decision tree
1. Read the exact event message.
   - `manifest unknown` / `not found`: the tag or digest does not exist. Typo in the tag,
     a CI job that never pushed, or a tag that was deleted.
   - `pull access denied` / `unauthorized`: credentials. Check `imagePullSecrets` and
     whether the secret exists in `shop` (you cannot read its contents, only its presence).
   - `i/o timeout` / `TLS handshake timeout`: registry or network problem. Check
     whether pods on other nodes can pull; see the DNS runbook if the host does not resolve.
2. Did the image field change in the latest revision? Compare with the previous
   revision. If the previous image works, the rollout introduced the problem.
3. Is the same image healthy in another environment? Then suspect pull credentials or
   node-local network, not the tag.

## Remediation options
- **Bad tag in a rollout:** roll back the Deployment (`rollback_deployment`) or set the
  container image to the last known good tag (`set_container_image`).
- **Missing pull secret:** hand off to team-platform; secrets are not managed by on-call.
- **Registry outage:** pause rollouts and wait; the existing pods keep running if they
  were not replaced.

## Do NOT
- Do not switch to `:latest` to "make it pull"; it hides which code is running.
- Do not set `imagePullPolicy: Always` as a fix; it adds a registry dependency to every restart.
- Do not delete the Deployment.

## Verify recovery
- `kubectl -n shop rollout status deploy/<deployment>` succeeds.
- The pod shows the expected image and `Running` with 1/1 Ready.
- Upstream services that call it recover (orders-api is called by nobody, but its
  absence shows up in checkout traffic).

## Escalation
Owning team for a bad tag; team-platform for registry or credential problems.

## Related
- [rb-bad-rollout](rb-bad-rollout.md)
- [pm-2025-11-orders-bad-image-tag](../postmortems/pm-2025-11-orders-bad-image-tag.md)
- [k8s-images](../k8s-docs/k8s-images.md)
