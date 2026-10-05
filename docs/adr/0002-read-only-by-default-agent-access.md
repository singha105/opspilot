# ADR-0002: Read-only by default agent access

- Status: accepted
- Date: 2026-10-05

## Context

An incident agent reads a lot of text it does not control: pod logs, Kubernetes
events, alert annotations and retrieved documents. Any of that text could contain
instructions meant to manipulate the model (prompt injection). If the agent's
Kubernetes identity can write, a successful injection becomes a cluster change.
Hiring managers will reasonably ask how the design limits that blast radius.

## Decision

Kubernetes permissions are enforced by RBAC, not by prompts.

- The agent investigates only as ServiceAccount `opspilot-reader`. Its ClusterRole
  allows get/list/watch on pods, pod logs, events, Deployments, ReplicaSets,
  Services, Endpoints, EndpointSlices, ConfigMaps, nodes, PVCs and namespaces.
  It grants no Secrets, no `pods/exec` and no write verbs.
- Changes go through a separate identity, `opspilot-operator`. It has a Role in
  namespace `shop` only, with get/patch on Deployments and `deployments/scale`.
  Only the gated actions MCP server (Day 3) uses it, and only with a single-use
  approval token issued after a human decision.
- Both identities use short-lived tokens (`kubectl create token`, 12 h), written
  to kubeconfigs in the gitignored `.secrets/` directory. Agent code always names
  its kubeconfig explicitly and never falls back to the default context.
- Fault injection uses the admin context, which is never available to the agent
  (see ADR-0003).

## Alternatives considered

- **A single identity plus prompt rules.** Simple, but one successful injection
  could change the cluster.
- **Namespace-scoped reader.** Tighter, but diagnoses often need nodes, PVCs and
  events from outside the namespace. A cluster-wide read-only role without Secrets
  is a reasonable middle ground for a demo cluster.
- **An admission webhook or policy engine.** Useful in production, but more than a
  single-node demo needs. RBAC already gives a hard boundary.

## Consequences

- Integration tests prove the boundary. A `kubectl auth can-i` matrix covers both
  identities, including subresources via `--subresource`. Real API calls made
  with the reader kubeconfig must get 403 for listing Secrets, deleting a pod,
  patching a Deployment and exec.
- ConfigMaps are readable. The demo keeps secrets out of ConfigMaps, and the
  security doc (Day 6) will call this out.
- Tokens expire, so `make kubeconfigs` must be re-run in each new session.
