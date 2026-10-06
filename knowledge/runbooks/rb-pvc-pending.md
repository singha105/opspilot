---
id: rb-pvc-pending
title: PersistentVolumeClaim stuck Pending and pods waiting on volumes
doc_type: runbook
categories: [PVC_PENDING]
services: [redis]
tags: [storage, pvc, storageclass, volumes, pending]
severity: medium
last_reviewed: 2026-07-12
related: [rb-scheduling-constraints, k8s-persistent-volumes]
---
# PersistentVolumeClaim stuck Pending and pods waiting on volumes

## Symptoms
- `kubectl get pvc` shows `Pending` for minutes.
- The pod that mounts it is `Pending` with `FailedScheduling`:
  `0/1 nodes are available: pod has unbound immediate PersistentVolumeClaims`, or sits
  in `ContainerCreating` with `FailedMount` / `FailedAttachVolume`.
- PVC events: `storageclass.storage.k8s.io "<name>" not found`,
  `waiting for first consumer to be created before binding`, or
  `no persistent volumes available for this claim`.

## Quick checks (read-only)
```bash
kubectl -n shop get pvc
kubectl -n shop describe pvc <claim>
kubectl get storageclass
kubectl get pv
kubectl -n shop describe pod <pod> | sed -n '/Volumes/,$p'
```
The demo cluster provides one StorageClass, `local-path` (default, binding mode
`WaitForFirstConsumer`). Shopfront redis normally runs with an `emptyDir`; a PVC only
appears when someone enables persistence.

## Diagnosis decision tree
1. Does the PVC name a StorageClass that exists? A typo or a class from another cluster
   (`gp3`, `standard`) leaves it Pending forever.
2. Binding mode `WaitForFirstConsumer` and no pod scheduled yet? Pending is normal until
   a pod using it is scheduled; look at why the pod is not scheduling instead.
3. Static provisioning (no class): is there a PV with matching size and access mode?
   `ReadWriteMany` is not supported by `local-path`.
4. Is the requested size larger than the node can provide?

## Remediation options
- Correct the StorageClass name or access mode in the manifest (owning team), then
  re-create the claim; most claim fields are immutable.
- Roll back the revision that introduced the claim if persistence was not intended.

## Do NOT
- Do not delete a bound PVC to "reset" it; data is lost with the `Delete` reclaim policy.
- Do not create PVs by hand pointing at arbitrary host paths.

## Verify recovery
- PVC `Bound`, pod scheduled and Ready, no `FailedMount` events.

## Escalation
team-platform owns storage classes and node disks.

## Related
- [rb-scheduling-constraints](rb-scheduling-constraints.md)
- [k8s-persistent-volumes](../k8s-docs/k8s-persistent-volumes.md)
