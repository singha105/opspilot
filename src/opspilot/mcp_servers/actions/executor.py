"""Plan (server-side dry run) and execute allowlisted actions.

Reads that the narrow operator Role cannot do (ReplicaSets for rollback and image
history) use the read-only reader identity. Every write uses the operator identity.
"""

import datetime as dt
from typing import Any

from opspilot.mcp_servers.actions.models import (
    Action,
    PatchContainerResources,
    RestartDeployment,
    RollbackDeployment,
    ScaleDeployment,
    SetContainerImage,
)
from opspilot.mcp_servers.common.errors import ToolError
from opspilot.mcp_servers.common.kube import KubeApis
from opspilot.mcp_servers.common.runner import REQUEST_TIMEOUT_S
from opspilot.mcp_servers.k8s_readonly.backend import K8sReadBackend

STRATEGIC = "application/strategic-merge-patch+json"
JSON_PATCH = "application/json-patch+json"
RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"

RISKS = {
    "restart_deployment": "Pods are replaced one at a time with maxSurge 0: a single-replica "
    "service is unavailable for a few seconds while its pod restarts.",
    "rollback_deployment": "The pod template returns to an earlier revision; any config or image "
    "change made since then is undone.",
    "scale_deployment": "Changes capacity immediately. Scaling to 0 stops the service.",
    "patch_container_resources": "Changing limits restarts the pods; a limit below real usage "
    "causes OOM kills or CPU throttling.",
    "set_container_image": "Rolls the pods to another image already used by this deployment.",
}


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts/lists into dotted paths (lists of named items use the name)."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            label = item.get("name", index) if isinstance(item, dict) else index
            out.update(flatten(item, f"{prefix}[{label}]"))
    else:
        out[prefix] = value
    return out


def spec_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Human-readable changes to replicas and the pod template."""

    def relevant(d: dict[str, Any]) -> dict[str, Any]:
        spec = d.get("spec", {})
        template = dict(spec.get("template", {}))
        meta = dict(template.get("metadata", {}))
        labels = {k: v for k, v in (meta.get("labels") or {}).items() if k != "pod-template-hash"}
        meta["labels"] = labels
        template["metadata"] = meta
        return flatten({"replicas": spec.get("replicas"), "template": template})

    old, new = relevant(before), relevant(after)
    return [
        {"path": path, "before": old.get(path), "after": new.get(path)}
        for path in sorted(set(old) | set(new))
        if old.get(path) != new.get(path)
    ]


class ActionExecutor:
    """Builds and applies the patch for each allowlisted action."""

    def __init__(self, operator: KubeApis, reader: K8sReadBackend) -> None:
        self.operator = operator
        self.reader = reader

    def _deployment(self, action: Action) -> dict[str, Any]:
        d = self.operator.apps.read_namespaced_deployment(
            action.deployment, action.namespace, _request_timeout=REQUEST_TIMEOUT_S
        )
        data: dict[str, Any] = self.operator.api.sanitize_for_serialization(d)
        return data

    def _container_names(self, deployment: dict[str, Any]) -> list[str]:
        return [c["name"] for c in deployment["spec"]["template"]["spec"]["containers"]]

    def _rollback_template(self, action: RollbackDeployment) -> dict[str, Any]:
        deployment, owned = self.reader.replica_sets(action.namespace, action.deployment)
        current = int(
            (deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision", 0)
        )
        by_revision = {
            int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", 0)): rs
            for rs in owned
        }
        if action.to_revision is not None:
            target = action.to_revision
        else:
            earlier = [r for r in by_revision if r < current]
            if not earlier:
                raise ToolError("invalid_argument", "There is no earlier revision to roll back to.")
            target = max(earlier)
        if target not in by_revision:
            raise ToolError(
                "invalid_argument",
                f"Revision {target} does not exist.",
                f"Known revisions: {sorted(by_revision)}.",
            )
        if target == current:
            raise ToolError("invalid_argument", f"Revision {target} is already current.")
        template: dict[str, Any] = self.operator.api.sanitize_for_serialization(
            by_revision[target].spec.template
        )
        (template.get("metadata", {}).get("labels") or {}).pop("pod-template-hash", None)
        return template

    def _patch(self, action: Action, deployment: dict[str, Any]) -> tuple[Any, str]:
        if isinstance(action, RestartDeployment):
            stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            body = {"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: stamp}}}}}
            return body, STRATEGIC
        if isinstance(action, RollbackDeployment):
            return [
                {
                    "op": "replace",
                    "path": "/spec/template",
                    "value": self._rollback_template(action),
                }
            ], JSON_PATCH
        if isinstance(action, ScaleDeployment):
            return {"spec": {"replicas": action.replicas}}, STRATEGIC
        names = self._container_names(deployment)
        if action.container not in names:
            raise ToolError(
                "invalid_argument",
                f"Container {action.container!r} not in {action.deployment}.",
                f"Containers: {', '.join(names)}.",
            )
        if isinstance(action, PatchContainerResources):
            limits = {
                k: v for k, v in (("memory", action.memory_limit), ("cpu", action.cpu_limit)) if v
            }
            container = {"name": action.container, "resources": {"limits": limits}}
        else:
            assert isinstance(action, SetContainerImage)
            history = self.reader.get_rollout_history(action.namespace, action.deployment)
            known = {image for rev in history.revisions for image in rev.images}
            if action.image not in known:
                raise ToolError(
                    "out_of_bounds",
                    f"Image {action.image!r} never ran in {action.deployment}.",
                    f"Images in the rollout history: {', '.join(sorted(known))}.",
                )
            container = {"name": action.container, "image": action.image}
        return {"spec": {"template": {"spec": {"containers": [container]}}}}, STRATEGIC

    def _apply(self, action: Action, dry_run: bool) -> dict[str, Any]:
        before = self._deployment(action)
        body, content_type = self._patch(action, before)
        kwargs: dict[str, Any] = {
            "_request_timeout": REQUEST_TIMEOUT_S,
            "_content_type": content_type,
        }
        if dry_run:
            kwargs["dry_run"] = "All"
        result = self.operator.apps.patch_namespaced_deployment(
            action.deployment, action.namespace, body, **kwargs
        )
        after: dict[str, Any] = self.operator.api.sanitize_for_serialization(result)
        return {"before": before, "after": after}

    def plan(self, action: Action, action_hash: str) -> dict[str, Any]:
        """Server-side dry run (dryRun=All): what would change, and the risks."""
        applied = self._apply(action, dry_run=True)
        changes = spec_diff(applied["before"], applied["after"])
        notes = [RISKS[action.type]]
        if isinstance(action, RestartDeployment):
            changes = [c for c in changes if not c["path"].endswith(RESTARTED_AT)] + [
                {
                    "path": f"template.metadata.annotations.{RESTARTED_AT}",
                    "before": None,
                    "after": "<time of execution>",
                }
            ]
        if not changes:
            notes.append("The dry run produced no change: this action would be a no-op.")
        return {
            "action": action.model_dump(exclude_none=True),
            "action_hash": action_hash,
            "dry_run": "passed",
            "diff": changes,
            "risk_notes": notes,
            "next_step": "A human must approve this exact action_hash before execute_action.",
        }

    def execute(self, action: Action, action_hash: str) -> dict[str, Any]:
        applied = self._apply(action, dry_run=False)
        after = applied["after"]
        return {
            "executed": True,
            "action": action.model_dump(exclude_none=True),
            "action_hash": action_hash,
            "diff": spec_diff(applied["before"], after),
            "generation": after.get("metadata", {}).get("generation"),
            "verify": f"Watch recovery with get_deployment(namespace='{action.namespace}', "
            f"name='{action.deployment}') and list_pods.",
        }
