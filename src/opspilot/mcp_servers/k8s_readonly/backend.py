"""Live, read-only Kubernetes backend. Exposes no write operation of any kind."""

import datetime as dt
from collections.abc import Callable, Sequence
from typing import Any

from opspilot.mcp_servers.common.kube import KubeApis
from opspilot.mcp_servers.common.runner import REQUEST_TIMEOUT_S
from opspilot.mcp_servers.k8s_readonly import models as m

MAX_LOG_LINE = 400
MAX_CONFIG_VALUE = 500
POD_EVENTS = 15

Clock = Callable[[], dt.datetime]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def human_age(seconds: float) -> str:
    """Compact age like kubectl: 45s, 12m, 3h, 2d."""
    s = int(max(seconds, 0))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def _state(state: Any) -> dict[str, Any]:
    """Condense a V1ContainerState into {waiting|running|terminated: {...}}."""
    if state is None:
        return {}
    if state.waiting:
        out = {"reason": state.waiting.reason}
        if state.waiting.message:
            out["message"] = state.waiting.message[:300]
        return {"waiting": out}
    if state.terminated:
        t = state.terminated
        out = {"reason": t.reason, "exitCode": t.exit_code}
        if t.message:
            out["message"] = t.message[:300]
        if t.finished_at:
            out["finishedAt"] = t.finished_at.isoformat()
        return {"terminated": out}
    if state.running:
        started = state.running.started_at
        return {"running": {"startedAt": started.isoformat() if started else None}}
    return {}


def _probe(probe: Any) -> str | None:
    if probe is None:
        return None
    if probe.http_get:
        target = f"GET {probe.http_get.path} :{probe.http_get.port}"
    elif probe.tcp_socket:
        target = f"TCP :{probe.tcp_socket.port}"
    elif getattr(probe, "_exec", None):  # the generated model names the field "_exec"
        target = "exec " + " ".join(probe._exec.command or [])
    elif probe.grpc:
        target = f"gRPC :{probe.grpc.port}"
    else:
        target = "unknown"
    return (
        f"{target} every {probe.period_seconds or 10}s, timeout {probe.timeout_seconds or 1}s, "
        f"fail after {probe.failure_threshold or 3}"
    )


def _resources(res: Any) -> dict[str, Any]:
    if res is None:
        return {}
    out: dict[str, Any] = {}
    if res.requests:
        out["requests"] = dict(res.requests)
    if res.limits:
        out["limits"] = dict(res.limits)
    return out


def _container_spec(c: Any) -> dict[str, Any]:
    """Spec fields shared by pod and deployment template views (env values are omitted)."""
    probes = {
        k: v
        for k, v in (
            ("liveness", _probe(c.liveness_probe)),
            ("readiness", _probe(c.readiness_probe)),
            ("startup", _probe(c.startup_probe)),
        )
        if v
    }
    env_from = []
    for source in c.env_from or []:
        if source.config_map_ref:
            env_from.append(f"configMap/{source.config_map_ref.name}")
        if source.secret_ref:
            env_from.append(f"secret/{source.secret_ref.name}")
    return {
        "name": c.name,
        "image": c.image,
        "resources": _resources(c.resources),
        "envNames": [e.name for e in c.env or []],
        "envFrom": env_from,
        "command": c.command,
        "args": c.args,
        "probes": probes,
        "volumeMounts": [f"{v.name}:{v.mount_path}" for v in c.volume_mounts or []],
    }


def _event_time(e: Any) -> dt.datetime | None:
    for value in (e.last_timestamp, getattr(e, "event_time", None), e.first_timestamp):
        if value:
            return value  # type: ignore[no-any-return]
    meta = e.metadata
    return meta.creation_timestamp if meta else None


def _labels_selector(labels: dict[str, str] | None) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted((labels or {}).items()))


class K8sReadBackend:
    """Implements every read tool against the API with the reader identity."""

    def __init__(self, apis: KubeApis, clock: Clock = _utcnow) -> None:
        self.apis = apis
        self.clock = clock

    # ---- helpers ------------------------------------------------------------------

    def _events(
        self, namespace: str, field_selector: str | None = None, since_minutes: int = 120
    ) -> list[m.EventItem]:
        kwargs: dict[str, Any] = {"_request_timeout": REQUEST_TIMEOUT_S}
        if field_selector:
            kwargs["field_selector"] = field_selector
        items = self.apis.core.list_namespaced_event(namespace, **kwargs).items
        now = self.clock()
        events: list[tuple[float, m.EventItem]] = []
        for e in items:
            when = _event_time(e)
            age = (now - when).total_seconds() if when else 0.0
            if age > since_minutes * 60:
                continue
            obj = e.involved_object
            events.append(
                (
                    age,
                    m.EventItem(
                        type=e.type or "Normal",
                        reason=e.reason or "",
                        object=f"{obj.kind}/{obj.name}" if obj else "",
                        count=e.count or 1,
                        message=(e.message or "")[:400],
                        age=human_age(age),
                        age_s=int(age),
                    ),
                )
            )
        events.sort(key=lambda pair: pair[0])
        return [ev for _, ev in events]

    # ---- tools ----------------------------------------------------------------------

    def list_pods(self, namespace: str, label_selector: str | None = None) -> m.PodList:
        kwargs: dict[str, Any] = {"_request_timeout": REQUEST_TIMEOUT_S}
        if label_selector:
            kwargs["label_selector"] = label_selector
        pods = self.apis.core.list_namespaced_pod(namespace, **kwargs).items
        now = self.clock()
        out = []
        for p in sorted(pods, key=lambda p: p.metadata.name):
            statuses = p.status.container_statuses or []
            reason = last_reason = None
            for cs in statuses:
                current = _state(cs.state)
                for key in ("waiting", "terminated"):
                    if key in current and current[key].get("reason"):
                        reason = reason or current[key]["reason"]
                last = _state(cs.last_state).get("terminated", {})
                last_reason = last_reason or last.get("reason")
            if p.metadata.deletion_timestamp:
                reason = "Terminating"
            created = p.metadata.creation_timestamp
            out.append(
                m.PodSummary(
                    name=p.metadata.name,
                    phase=p.status.phase or "Unknown",
                    ready=f"{sum(1 for c in statuses if c.ready)}/{len(p.spec.containers)}",
                    restarts=sum(c.restart_count or 0 for c in statuses),
                    reason=reason,
                    last_reason=last_reason,
                    age=human_age((now - created).total_seconds()) if created else "?",
                    node=p.spec.node_name,
                )
            )
        return m.PodList(namespace=namespace, pods=out)

    def describe_pod(self, namespace: str, name: str) -> m.PodDetail:
        p = self.apis.core.read_namespaced_pod(name, namespace, _request_timeout=REQUEST_TIMEOUT_S)
        statuses = {cs.name: cs for cs in p.status.container_statuses or []}
        init_statuses = {cs.name: cs for cs in p.status.init_container_statuses or []}

        def detail(c: Any, status_map: dict[str, Any]) -> m.ContainerDetail:
            cs = status_map.get(c.name)
            return m.ContainerDetail(
                **_container_spec(c),
                ready=bool(cs and cs.ready),
                restartCount=(cs.restart_count or 0) if cs else 0,
                state=_state(cs.state) if cs else {},
                lastState=_state(cs.last_state) if cs else {},
            )

        volumes = []
        for v in p.spec.volumes or []:
            kind = next(
                (
                    k
                    for k in (
                        "config_map",
                        "secret",
                        "empty_dir",
                        "persistent_volume_claim",
                        "projected",
                        "host_path",
                    )
                    if getattr(v, k, None) is not None
                ),
                "other",
            )
            volumes.append(f"{v.name} ({kind})")
        return m.PodDetail(
            pod=p.metadata.name,
            namespace=namespace,
            phase=p.status.phase or "Unknown",
            ready=all(cs.ready for cs in statuses.values()) and bool(statuses),
            node=p.spec.node_name,
            labels=dict(p.metadata.labels or {}),
            initContainers=[detail(c, init_statuses) for c in p.spec.init_containers or []],
            containers=[detail(c, statuses) for c in p.spec.containers],
            volumes=volumes,
            conditions=[
                {"type": c.type, "status": c.status, **({"reason": c.reason} if c.reason else {})}
                for c in p.status.conditions or []
            ],
            events=self._events(namespace, f"involvedObject.name={name}")[:POD_EVENTS],
        )

    def get_pod_logs(
        self,
        namespace: str,
        name: str,
        container: str | None = None,
        previous: bool = False,
        tail_lines: int = 100,
        contains: str | None = None,
    ) -> m.PodLogs:
        kwargs: dict[str, Any] = {
            "previous": previous,
            "tail_lines": tail_lines,
            "_request_timeout": REQUEST_TIMEOUT_S,
            # Raw response: the generated client otherwise returns a bytes repr as str.
            "_preload_content": False,
        }
        if container:
            kwargs["container"] = container
        response = self.apis.core.read_namespaced_pod_log(name, namespace, **kwargs)
        text = response.data.decode("utf-8", errors="replace")
        lines = [line[:MAX_LOG_LINE] for line in text.splitlines()]
        if contains:
            needle = contains.lower()
            lines = [line for line in lines if needle in line.lower()]
        return m.PodLogs(
            pod=name,
            container=container,
            previous=previous,
            tail_lines=tail_lines,
            contains=contains,
            line_count=len(lines),
            lines=lines,
        )

    def get_events(
        self, namespace: str, involved_object_name: str | None = None, since_minutes: int = 60
    ) -> m.EventList:
        selector = f"involvedObject.name={involved_object_name}" if involved_object_name else None
        return m.EventList(
            namespace=namespace,
            involved_object_name=involved_object_name,
            since_minutes=since_minutes,
            events=self._events(namespace, selector, since_minutes),
        )

    def get_deployment(self, namespace: str, name: str) -> m.DeploymentDetail:
        d = self.apis.apps.read_namespaced_deployment(
            name, namespace, _request_timeout=REQUEST_TIMEOUT_S
        )
        s = d.status
        strategy = d.spec.strategy
        rolling = strategy.rolling_update if strategy else None
        strategy_text = strategy.type if strategy else "RollingUpdate"
        if rolling:
            strategy_text += (
                f" (maxSurge={rolling.max_surge}, maxUnavailable={rolling.max_unavailable})"
            )
        return m.DeploymentDetail(
            name=name,
            namespace=namespace,
            revision=(d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
            replicas={
                "desired": d.spec.replicas or 0,
                "ready": s.ready_replicas or 0,
                "updated": s.updated_replicas or 0,
                "available": s.available_replicas or 0,
                "unavailable": s.unavailable_replicas or 0,
            },
            strategy=strategy_text,
            selector=dict(d.spec.selector.match_labels or {}),
            conditions=[
                {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                for c in s.conditions or []
            ],
            template={
                "labels": dict(d.spec.template.metadata.labels or {}),
                "containers": [_container_spec(c) for c in d.spec.template.spec.containers],
                "initContainers": [
                    _container_spec(c) for c in d.spec.template.spec.init_containers or []
                ],
            },
        )

    def replica_sets(self, namespace: str, name: str) -> tuple[Any, list[Any]]:
        """The deployment and its owned ReplicaSets, sorted by revision (oldest first)."""
        d = self.apis.apps.read_namespaced_deployment(
            name, namespace, _request_timeout=REQUEST_TIMEOUT_S
        )
        selector = _labels_selector(d.spec.selector.match_labels)
        rs_items = self.apis.apps.list_namespaced_replica_set(
            namespace, label_selector=selector, _request_timeout=REQUEST_TIMEOUT_S
        ).items
        owned = [
            rs
            for rs in rs_items
            if any(o.uid == d.metadata.uid for o in rs.metadata.owner_references or [])
        ]

        def revision(rs: Any) -> int:
            return int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", 0))

        return d, sorted(owned, key=revision)

    def get_rollout_history(self, namespace: str, name: str) -> m.RolloutHistory:
        d, owned = self.replica_sets(namespace, name)
        revisions = []
        for rs in owned:
            ann = rs.metadata.annotations or {}
            revisions.append(
                m.RolloutRevision(
                    revision=int(ann.get("deployment.kubernetes.io/revision", 0)),
                    replicaset=rs.metadata.name,
                    images=[c.image for c in rs.spec.template.spec.containers],
                    change_cause=ann.get("kubernetes.io/change-cause"),
                    replicas=rs.status.replicas or 0,
                    ready=rs.status.ready_replicas or 0,
                    created=rs.metadata.creation_timestamp.isoformat()
                    if rs.metadata.creation_timestamp
                    else "",
                    template_hash=(rs.metadata.labels or {}).get("pod-template-hash"),
                )
            )
        return m.RolloutHistory(
            deployment=name,
            namespace=namespace,
            current_revision=(d.metadata.annotations or {}).get(
                "deployment.kubernetes.io/revision"
            ),
            revisions=revisions,
        )

    def list_services(self, namespace: str) -> m.ServiceList:
        items = self.apis.core.list_namespaced_service(
            namespace, _request_timeout=REQUEST_TIMEOUT_S
        ).items
        return m.ServiceList(
            namespace=namespace,
            services=[
                m.ServiceInfo(
                    name=s.metadata.name,
                    type=s.spec.type or "ClusterIP",
                    cluster_ip=s.spec.cluster_ip,
                    selector=dict(s.spec.selector or {}),
                    ports=[
                        f"{p.name or ''}:{p.port}->{p.target_port}/{p.protocol}"
                        for p in s.spec.ports or []
                    ],
                )
                for s in sorted(items, key=lambda s: s.metadata.name)
            ],
        )

    def get_service_endpoints(self, namespace: str, name: str) -> m.ServiceEndpoints:
        slices = self.apis.discovery.list_namespaced_endpoint_slice(
            namespace,
            label_selector=f"kubernetes.io/service-name={name}",
            _request_timeout=REQUEST_TIMEOUT_S,
        ).items
        addresses: list[m.EndpointAddress] = []
        ports: set[str] = set()
        for sl in slices:
            for p in sl.ports or []:
                ports.add(f"{p.name or ''}:{p.port}/{p.protocol}")
            for ep in sl.endpoints or []:
                ready = bool(ep.conditions and ep.conditions.ready)
                pod = ep.target_ref.name if ep.target_ref else None
                addresses.extend(
                    m.EndpointAddress(ip=ip, ready=ready, pod=pod) for ip in ep.addresses
                )
        return m.ServiceEndpoints(
            service=name,
            namespace=namespace,
            ready=sum(1 for a in addresses if a.ready),
            not_ready=sum(1 for a in addresses if not a.ready),
            ports=sorted(ports),
            addresses=addresses,
        )

    def get_configmap(self, namespace: str, name: str) -> m.ConfigMapView:
        cm = self.apis.core.read_namespaced_config_map(
            name, namespace, _request_timeout=REQUEST_TIMEOUT_S
        )
        data = {
            k: (v if len(v) <= MAX_CONFIG_VALUE else v[:MAX_CONFIG_VALUE] + "…")
            for k, v in sorted((cm.data or {}).items())
        }
        return m.ConfigMapView(
            name=name, namespace=namespace, data=data, binary_keys=sorted(cm.binary_data or {})
        )

    def list_nodes(self) -> m.NodeList:
        items = self.apis.core.list_node(_request_timeout=REQUEST_TIMEOUT_S).items
        return m.NodeList(
            nodes=[
                m.NodeInfo(
                    name=n.metadata.name,
                    kubelet=n.status.node_info.kubelet_version if n.status.node_info else None,
                    capacity=dict(n.status.capacity or {}),
                    allocatable=dict(n.status.allocatable or {}),
                    taints=[f"{t.key}={t.value or ''}:{t.effect}" for t in n.spec.taints or []],
                    labels=dict(n.metadata.labels or {}),
                    conditions=[
                        {"type": c.type, "status": c.status, "reason": c.reason}
                        for c in n.status.conditions or []
                    ],
                )
                for n in items
            ]
        )

    def list_pvcs(self, namespace: str) -> m.PvcList:
        items = self.apis.core.list_namespaced_persistent_volume_claim(
            namespace, _request_timeout=REQUEST_TIMEOUT_S
        ).items
        events = self._events(namespace) if items else []
        return m.PvcList(
            namespace=namespace,
            pvcs=[
                m.PvcInfo(
                    name=c.metadata.name,
                    status=c.status.phase or "Unknown",
                    storage_class=c.spec.storage_class_name,
                    access_modes=list(c.spec.access_modes or []),
                    requested=(c.spec.resources.requests or {}).get("storage")
                    if c.spec.resources
                    else None,
                    capacity=(c.status.capacity or {}).get("storage"),
                    volume=c.spec.volume_name,
                    events=[
                        e for e in events if e.object == f"PersistentVolumeClaim/{c.metadata.name}"
                    ],
                )
                for c in items
            ],
        )

    def configmap_names(self, namespace: str) -> Sequence[str]:
        """ConfigMap names (used by the recorder to call get_configmap exhaustively)."""
        items = self.apis.core.list_namespaced_config_map(
            namespace, _request_timeout=REQUEST_TIMEOUT_S
        ).items
        return sorted(cm.metadata.name for cm in items)

    def server_version(self) -> str:
        info = self.apis.version.get_code(_request_timeout=REQUEST_TIMEOUT_S)
        return str(info.git_version)
