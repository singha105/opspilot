# ADR-0007: Single-use approval tokens for write actions

- Status: accepted
- Date: 2026-10-07

## Context

OpsPilot may propose a fix, but a human decides. "The agent asked and the human said
yes" has to be something the actions server can verify for itself, not something it
trusts the agent (or a prompt) to report. The check must bind the approval to one exact
action, expire quickly and be impossible to reuse.

## Decision

- **Two phases.** `plan_action` validates the action, runs the patch server-side with
  `dryRun=All` and returns a diff, risk notes and `action_hash` = sha256 of the canonical
  action JSON (sorted keys, no whitespace, nulls dropped). It needs no token and
  changes nothing.
- **Minting.** After a human approves, `opspilot.agent.approval.mint_approval_token`
  creates `v1.<claims>.<signature>`, where the signature is HMAC-SHA256 over
  `action_hash|expiry|approver|nonce` with `OPSPILOT_APPROVAL_SECRET` (at least 32
  characters, read from the gitignored `.secrets/.env`). Lifetime is at most 600 s.
  This module is the only code that mints tokens; the MCP servers can only verify.
- **Verification, in order.** Format and version, signature (constant-time compare),
  the token's hash equals the hash of the action being executed, not expired, expiry no
  more than 10 minutes ahead, and the nonce has never been used. The nonce is burned in
  a SQLite store (`runs/approval_nonces.sqlite`) before the patch is sent, so a token
  works exactly once even across server restarts.
- **Server-side bounds before the token is checked.** The action must be one of five
  allowlisted types in an allowlisted namespace, with replicas 0–5, memory ≤ 512Mi,
  cpu ≤ 1 core, and `set_container_image` only to an image already in that Deployment's
  rollout history. A valid token cannot authorise an out-of-bounds action.
- **Audit.** Every attempt, including rejections, is logged with the redacted action,
  the unverified approver and the first 8 characters of the nonce. The token itself is
  never logged.

## Alternatives considered

- **Trusting an `approved: true` flag from the agent.** Any prompt injection could set it.
- **Asymmetric signatures (Ed25519).** Better if approval and execution ran on different
  machines; for one host a shared HMAC secret is simpler and equally strong.
- **Kubernetes-native approval (admission webhook or a CRD workflow).** Closer to
  production patterns, but much heavier than this project needs. The action allowlist
  and the operator Role already bound the blast radius.

## Consequences

- Unit tests prove rejection of missing, malformed, wrong-version, tampered (signature
  and claims), expired, too-long-lived, wrong-hash and reused tokens, plus out-of-bounds
  arguments and non-allowlisted namespaces. An integration test executes a real restart
  with a valid token and shows that reusing the token fails.
- A planned action must be executed exactly as planned: any change to the action changes
  its hash and invalidates the approval.
- Rotating the secret invalidates all outstanding tokens, which is the intended behaviour.
- The secret must be created once per machine:
  `echo "OPSPILOT_APPROVAL_SECRET=$(openssl rand -hex 32)" >> .secrets/.env`.
