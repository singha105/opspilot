---
id: rb-missing-configmap-or-secret
title: CreateContainerConfigError from a missing ConfigMap, Secret or key
doc_type: runbook
categories: [MISSING_CONFIGMAP_OR_SECRET]
services: [payments-api, orders-api, inventory-api]
tags: [configmap, secret, config, container-config]
severity: high
last_reviewed: 2026-08-20
related: [rb-missing-env-var, k8s-configure-pod-configmap]
---
# CreateContainerConfigError from a missing ConfigMap, Secret or key

## Symptoms
- Pod is stuck in `CreateContainerConfigError` (or `CreateContainerError`); the
  container never starts, so there are no application logs at all.
- Events say `configmap "<name>" not found`, `secret "<name>" not found` or
  `couldn't find key <KEY> in ConfigMap shop/<name>`.
- Pods that mount the object as a volume may instead sit in `ContainerCreating` with
  `MountVolume.SetUp failed`.

## Quick checks (read-only)
```bash
kubectl -n shop get pods
kubectl -n shop describe pod <pod> | sed -n '/Events/,$p'
kubectl -n shop get configmaps
kubectl -n shop get configmap shopfront-settings -o jsonpath='{.data}'
kubectl -n shop get deploy <deployment> -o yaml | grep -n -A3 -E 'configMapKeyRef|secretKeyRef|configMapRef|secretRef'
kubectl -n shop get events --sort-by=.lastTimestamp | tail -20
```
OpsPilot's reader identity can list ConfigMaps but cannot read Secrets. To check a
Secret, rely on the event text (`secret "x" not found`) and on the Deployment spec.

## Diagnosis decision tree
1. Is the pod in `CreateContainerConfigError`? If it is `CrashLoopBackOff` with logs,
   the object exists and this is a different problem (see the missing env runbook).
2. Which reference is broken? The event names the object and, for keys, the key.
3. Does the object exist in `shop`? A ConfigMap created in the wrong namespace is a
   common cause; references never cross namespaces.
4. Does it exist but lack the key? Someone renamed a key (for example `REDIS_URL` to
   `REDIS_ADDR`) without updating every consumer.
5. Was the object deleted by a cleanup job or a kustomize change that dropped it from
   the resource list? Check recent changes in the deploy pipeline.

## Remediation options
- Re-create the ConfigMap or key from the base manifests (team-platform owns
  `shopfront-settings`).
- If a Deployment revision points at a new name that was never created, roll back the
  Deployment (`rollback_deployment`).
- Mark a reference `optional: true` only if the service genuinely works without it.

## Do NOT
- Do not copy Secret values into a ConfigMap to unblock a pod.
- Do not edit `shopfront-settings` by hand in production without a change record; three
  services read it.

## Verify recovery
- Pod transitions to `Running`, then Ready.
- `kubectl -n shop describe pod` shows no new `Failed` events.
- All consumers of the ConfigMap restart cleanly (ConfigMap env values are read only
  at container start).

## Escalation
team-platform for shared ConfigMaps and Secrets; the owning team for service-specific ones.

## Related
- [rb-missing-env-var](rb-missing-env-var.md)
- [k8s-configure-pod-configmap](../k8s-docs/k8s-configure-pod-configmap.md)
