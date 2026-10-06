---
id: rb-tls-certificate-expiry
title: TLS certificate expired or about to expire
doc_type: runbook
categories: []
services: [orders-api, payments-api]
tags: [tls, certificates, cert-manager, https]
severity: high
last_reviewed: 2026-06-12
related: [rb-ingress-502]
---
# TLS certificate expired or about to expire

## Symptoms
- Clients fail with `x509: certificate has expired or is not yet valid`,
  `SSL: CERTIFICATE_VERIFY_FAILED` or browser warnings for the shop domain.
- Alert `CertificateExpiringSoon` (fires 14 days before expiry) or
  `CertificateNotReady` from cert-manager.
- Pods and Services are healthy; the failure is at the TLS layer only.

## Quick checks (read-only)
```bash
kubectl -n shop get certificates
kubectl -n shop describe certificate <name>
kubectl -n shop get certificaterequests,orders,challenges
kubectl -n cert-manager logs deploy/cert-manager --tail=50
kubectl -n shop get ingress -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.tls}{"\n"}{end}'
```
Shopfront's public certificate for the storefront is issued by cert-manager with an
ACME issuer and renewed 30 days before expiry. Internal service-to-service traffic is
plain HTTP inside the cluster.

## Diagnosis decision tree
1. Is the served certificate actually expired? Check `notAfter` with
   `openssl s_client -connect <host>:443 -servername <host> | openssl x509 -noout -dates`.
2. Is the `Certificate` resource `Ready=False`? Read its conditions and the latest
   `CertificateRequest` and ACME `Challenge`.
3. Challenge failing? HTTP-01 challenges fail when the ingress cannot route
   `/.well-known/acme-challenge/` or DNS points elsewhere.
4. Certificate renewed but clients still see the old one? The ingress controller may
   not have reloaded the Secret.
5. Only some clients fail? Older clients may not trust a new intermediate certificate
   in the chain; compare the full chain the server sends with what the client trusts.
6. Rate limited by the ACME provider (`too many certificates already issued`)? Repeated
   failed renewals in a loop can exhaust the weekly quota; stop the loop first.

## Remediation options
- Fix the challenge path or issuer configuration with team-platform, then let
  cert-manager retry.
- As a last resort team-platform can issue a certificate manually.

## Do NOT
- Do not turn off certificate verification in clients.
- Do not copy certificate Secrets between namespaces by hand.

## Verify recovery
- `Certificate` is `Ready=True` with a new `notAfter`; external checks pass.

## Escalation
team-platform owns cert-manager and ingress.

## Related
- [rb-ingress-502](rb-ingress-502.md)
