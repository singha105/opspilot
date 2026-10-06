---
id: rb-bad-command
title: Container exits immediately because of a wrong command or arguments
doc_type: runbook
categories: [BAD_COMMAND]
services: [payments-api, orders-api, inventory-api, redis]
tags: [command, args, entrypoint, crashloop, exit-code]
severity: medium
last_reviewed: 2026-07-25
related: [rb-missing-env-var, rb-oom-killed, k8s-determine-reason-pod-failure, k8s-pod-lifecycle]
---
# Container exits immediately because of a wrong command or arguments

## Symptoms
- `CrashLoopBackOff` with very short-lived containers.
- Exit code 127 (`command not found`), 126 (permission denied / not executable),
  2 (usage error) or 1 with an argument-parsing message.
- Status may show `RunContainerError` or `StartError` with
  `exec: "<cmd>": executable file not found in $PATH`.
- Logs are empty or contain only a usage line such as `unrecognized arguments`.

## Quick checks (read-only)
```bash
kubectl -n shop describe pod <pod> | grep -A10 'Last State'
kubectl -n shop logs <pod> --previous
kubectl -n shop get deploy <deployment> -o jsonpath='{.spec.template.spec.containers[0].command}{"\n"}{.spec.template.spec.containers[0].args}'
kubectl -n shop get pod <pod> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.message}'
kubectl -n shop rollout history deploy/<deployment>
```
Shopfront app images start with `python /app/shopfront_svc.py` from the image
ENTRYPOINT; the Deployments do not set `command` or `args`. Redis runs
`redis-server` with `--save "" --appendonly no --maxmemory 32mb`.

## Diagnosis decision tree
1. Is the exit code 137 with `OOMKilled`? Use the OOM runbook.
2. Does the previous log show `FATAL missing required env`? Use the missing env runbook.
3. Exit 126/127 or `executable file not found`: a `command` override points at a binary
   that is not in the image (a new base image, or a typo).
4. Exit 1/2 with a usage message: `args` contain a flag the program does not accept,
   for example a redis flag spelled wrong.
5. Did `command`/`args` change in the latest revision? Compare revisions.

## Remediation options
- Remove the bad `command`/`args` override or roll back (`rollback_deployment`).

## Do NOT
- Do not replace the command with `sleep infinity` to keep the pod "up"; it hides the
  outage from probes while serving nothing.

## Verify recovery
- Container stays Running; restarts stop; readiness passes.

## Escalation
Owning team of the manifest change.

## Related
- [rb-missing-env-var](rb-missing-env-var.md)
- [rb-oom-killed](rb-oom-killed.md)
- [k8s-determine-reason-pod-failure](../k8s-docs/k8s-determine-reason-pod-failure.md)
- [k8s-pod-lifecycle](../k8s-docs/k8s-pod-lifecycle.md)
