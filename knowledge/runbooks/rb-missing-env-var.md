---
id: rb-missing-env-var
title: Service exits at startup because a required environment variable is missing
doc_type: runbook
categories: [CONFIG_MISSING_ENV]
services: [payments-api, orders-api, inventory-api]
tags: [env, config, crashloop, startup]
severity: high
last_reviewed: 2026-08-20
related: [rb-missing-configmap-or-secret, rb-bad-command, svc-inventory-api, pm-2026-01-inventory-missing-redis-url]
---
# Service exits at startup because a required environment variable is missing

## Symptoms
- Pod goes to `CrashLoopBackOff` seconds after each start; exit code 1, reason `Error`.
- The last log line before exit is a FATAL message naming the variable, for example
  `FATAL missing required env REDIS_URL`.
- No OOM, no probe failures in events: the process never gets far enough to serve.

## Quick checks (read-only)
```bash
kubectl -n shop get pods
kubectl -n shop logs <pod> --previous --tail=20
kubectl -n shop get deploy <deployment> -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}{"\n"}{end}'
kubectl -n shop get deploy <deployment> -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="REQUIRED_ENV")].value}'
kubectl -n shop get configmap shopfront-settings -o yaml
kubectl -n shop rollout history deploy/<deployment>
```
Shopfront services declare their mandatory variables in `REQUIRED_ENV` (a comma list).
`inventory-api` requires `REDIS_URL`; `orders-api` requires `REDIS_URL` and
`PAYMENTS_URL`. Values normally come from the `shopfront-settings` ConfigMap through
`configMapKeyRef`.

## Diagnosis decision tree
1. Does the previous container log a `FATAL missing required env <NAME>` line? That
   names the variable. If the log instead shows a stack trace or `exec format error`,
   use the bad-command runbook.
2. Is `<NAME>` present in the Deployment's `env` list?
   - Missing from the template: someone removed it in the last revision. Compare with
     `rollout history --revision=N`.
   - Present with `valueFrom.configMapKeyRef`: check the ConfigMap has the key. A missing
     key or missing ConfigMap normally stops the pod with `CreateContainerConfigError`
     instead; see the ConfigMap/Secret runbook.
3. Present but empty? An empty value counts as missing for Shopfront services.

## Remediation options
- Restore the variable from the base manifest (`restore_env_var`), referencing the
  `shopfront-settings` key rather than hard-coding a URL.
- If a whole revision is bad, roll back (`rollback_deployment`).

## Do NOT
- Do not remove the variable from `REQUIRED_ENV` to make the pod start; the service
  will then fail later on its first redis call.
- Do not paste credentials into env values; secrets belong in Secrets.

## Verify recovery
- New pod starts, logs `service started`, then reports Ready within about 10 seconds.
- `kubectl -n shop get pods` shows restarts no longer increasing.
- Dependency calls in the logs return `200`.

## Escalation
Owning team of the service. If the ConfigMap itself changed, include team-platform,
which owns `shopfront-settings`.

## Related
- [rb-missing-configmap-or-secret](rb-missing-configmap-or-secret.md)
- [rb-bad-command](rb-bad-command.md)
- [svc-inventory-api](../services/svc-inventory-api.md)
- [pm-2026-01-inventory-missing-redis-url](../postmortems/pm-2026-01-inventory-missing-redis-url.md)
- [k8s-configure-pod-configmap](../k8s-docs/k8s-configure-pod-configmap.md)
