import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException

from opspilot.mcp_servers.common import AuditLog, ToolRunner
from opspilot.mcp_servers.common.kube import KubeApis
from opspilot.mcp_servers.k8s_readonly.backend import K8sReadBackend, human_age
from opspilot.mcp_servers.k8s_readonly.server import DESCRIPTIONS, K8sTools, build_server

NOW = dt.datetime(2026, 10, 7, 14, 0, 0, tzinfo=dt.UTC)
_api = ApiClient()


def k8s(data: dict[str, Any], kind: str) -> Any:
    """Build a real kubernetes model object from API JSON."""
    return _api._ApiClient__deserialize(data, kind)  # type: ignore[attr-defined]


def ts(minutes_ago: int) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


CONTAINER_SPEC = {
    "name": "app",
    "image": "opspilot-demo-svc:dev",
    "env": [
        {"name": "SERVICE_NAME", "value": "payments-api"},
        {"name": "MEMORY_BALLAST_MB", "value": "300"},
    ],
    "resources": {
        "requests": {"memory": "32Mi", "cpu": "10m"},
        "limits": {"memory": "128Mi", "cpu": "200m"},
    },
    "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 10},
    "readinessProbe": {
        "httpGet": {"path": "/readyz", "port": "http"},
        "periodSeconds": 5,
        "failureThreshold": 2,
    },
}
POD = {
    "metadata": {
        "name": "payments-api-7c9d8b6f5-x2k4p",
        "namespace": "shop",
        "creationTimestamp": ts(30),
        "labels": {"app.kubernetes.io/name": "payments-api"},
    },
    "spec": {
        "containers": [CONTAINER_SPEC],
        "nodeName": "k3d-opspilot-server-0",
        "volumes": [{"name": "data", "emptyDir": {}}],
    },
    "status": {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}],
        "containerStatuses": [
            {
                "name": "app",
                "image": "opspilot-demo-svc:dev",
                "imageID": "x",
                "ready": False,
                "restartCount": 6,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            }
        ],
    },
}
EVENTS = {
    "items": [
        {
            "metadata": {"name": "e1", "namespace": "shop"},
            "type": "Warning",
            "reason": "BackOff",
            "count": 14,
            "message": "Back-off restarting failed container",
            "lastTimestamp": ts(1),
            "involvedObject": {"kind": "Pod", "name": "payments-api-7c9d8b6f5-x2k4p"},
        },
        {
            "metadata": {"name": "e2", "namespace": "shop"},
            "type": "Normal",
            "reason": "Pulled",
            "count": 1,
            "message": "pulled; DSN postgres://u:pw123456@db/x",
            "lastTimestamp": ts(5),
            "involvedObject": {"kind": "Pod", "name": "payments-api-7c9d8b6f5-x2k4p"},
        },
        {
            "metadata": {"name": "e3", "namespace": "shop"},
            "type": "Normal",
            "reason": "Old",
            "count": 1,
            "message": "too old",
            "lastTimestamp": ts(500),
            "involvedObject": {"kind": "Pod", "name": "payments-api-7c9d8b6f5-x2k4p"},
        },
    ]
}
DEPLOYMENT = {
    "metadata": {
        "name": "payments-api",
        "namespace": "shop",
        "uid": "dep-uid",
        "annotations": {"deployment.kubernetes.io/revision": "3"},
    },
    "spec": {
        "replicas": 1,
        "selector": {"matchLabels": {"app.kubernetes.io/name": "payments-api"}},
        "strategy": {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
        },
        "template": {
            "metadata": {"labels": {"app.kubernetes.io/name": "payments-api"}},
            "spec": {"containers": [CONTAINER_SPEC]},
        },
    },
    "status": {
        "readyReplicas": 0,
        "updatedReplicas": 1,
        "unavailableReplicas": 1,
        "conditions": [
            {"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable"}
        ],
    },
}


def rs(revision: int, image: str, owner: str = "dep-uid") -> dict[str, Any]:
    return {
        "metadata": {
            "name": f"payments-api-r{revision}",
            "namespace": "shop",
            "creationTimestamp": ts(60 - revision),
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
            "labels": {"pod-template-hash": f"h{revision}"},
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "payments-api",
                    "uid": owner,
                }
            ],
        },
        "spec": {
            "selector": {"matchLabels": {}},
            "template": {"spec": {"containers": [{"name": "app", "image": image}]}},
        },
        "status": {"replicas": 0, "readyReplicas": 0},
    }


class LogResponse:
    def __init__(self, text: str) -> None:
        self.data = text.encode()


class FakeCore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_namespaced_pod(self, namespace: str, **kw: Any) -> Any:
        self.calls.append(("list_namespaced_pod", kw))
        return k8s({"items": [POD]}, "V1PodList")

    def read_namespaced_pod(self, name: str, namespace: str, **kw: Any) -> Any:
        if name != POD["metadata"]["name"]:
            raise ApiException(status=404, reason="Not Found")
        return k8s(POD, "V1Pod")

    def list_namespaced_event(self, namespace: str, **kw: Any) -> Any:
        self.calls.append(("list_namespaced_event", kw))
        return k8s(EVENTS, "CoreV1EventList")

    def read_namespaced_pod_log(self, name: str, namespace: str, **kw: Any) -> Any:
        self.calls.append(("read_namespaced_pod_log", kw))
        lines = [f'{{"msg": "line {i}", "token": "abc{i}xyz"}}' for i in range(5)] + ["ERROR boom"]
        return LogResponse("\n".join(lines[-kw["tail_lines"] :]))

    def list_namespaced_service(self, namespace: str, **kw: Any) -> Any:
        return k8s(
            {
                "items": [
                    {
                        "metadata": {"name": "redis"},
                        "spec": {
                            "type": "ClusterIP",
                            "clusterIP": "10.43.0.9",
                            "selector": {"app.kubernetes.io/name": "redis"},
                            "ports": [
                                {
                                    "name": "redis",
                                    "port": 6379,
                                    "targetPort": "redis",
                                    "protocol": "TCP",
                                }
                            ],
                        },
                    }
                ]
            },
            "V1ServiceList",
        )

    def read_namespaced_config_map(self, name: str, namespace: str, **kw: Any) -> Any:
        return k8s(
            {
                "metadata": {"name": name},
                "data": {"REDIS_URL": "redis://default:hunter2xx@redis:6379", "BIG": "z" * 900},
            },
            "V1ConfigMap",
        )

    def list_node(self, **kw: Any) -> Any:
        return k8s(
            {
                "items": [
                    {
                        "metadata": {"name": "n1", "labels": {"k": "v"}},
                        "spec": {
                            "taints": [
                                {"key": "dedicated", "value": "batch", "effect": "NoSchedule"}
                            ]
                        },
                        "status": {
                            "capacity": {"cpu": "8", "memory": "4Gi"},
                            "allocatable": {"cpu": "8"},
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "nodeInfo": {
                                "kubeletVersion": "v1.35.5",
                                "architecture": "arm64",
                                "bootID": "",
                                "containerRuntimeVersion": "",
                                "kernelVersion": "",
                                "kubeProxyVersion": "",
                                "machineID": "",
                                "operatingSystem": "linux",
                                "osImage": "",
                                "systemUUID": "",
                            },
                        },
                    }
                ]
            },
            "V1NodeList",
        )

    def list_namespaced_persistent_volume_claim(self, namespace: str, **kw: Any) -> Any:
        return k8s(
            {
                "items": [
                    {
                        "metadata": {"name": "redis-data"},
                        "spec": {
                            "storageClassName": "gp3",
                            "accessModes": ["ReadWriteOnce"],
                            "resources": {"requests": {"storage": "1Gi"}},
                        },
                        "status": {"phase": "Pending"},
                    }
                ]
            },
            "V1PersistentVolumeClaimList",
        )

    def list_namespaced_config_map(self, namespace: str, **kw: Any) -> Any:
        return k8s(
            {"items": [{"metadata": {"name": "b"}}, {"metadata": {"name": "a"}}]}, "V1ConfigMapList"
        )


class FakeApps:
    def read_namespaced_deployment(self, name: str, namespace: str, **kw: Any) -> Any:
        return k8s(DEPLOYMENT, "V1Deployment")

    def list_namespaced_replica_set(self, namespace: str, **kw: Any) -> Any:
        assert kw["label_selector"] == "app.kubernetes.io/name=payments-api"
        items = [
            rs(3, "opspilot-demo-svc:dev"),
            rs(2, "opspilot-demo-svc:prev"),
            rs(9, "x", owner="other"),
        ]
        return k8s({"items": items}, "V1ReplicaSetList")


class FakeDiscovery:
    def list_namespaced_endpoint_slice(self, namespace: str, **kw: Any) -> Any:
        return k8s(
            {
                "items": [
                    {
                        "metadata": {"name": "redis-abc"},
                        "addressType": "IPv4",
                        "ports": [{"name": "redis", "port": 6379, "protocol": "TCP"}],
                        "endpoints": [
                            {
                                "addresses": ["10.42.0.5"],
                                "conditions": {"ready": False},
                                "targetRef": {"kind": "Pod", "name": "redis-1"},
                            }
                        ],
                    }
                ]
            },
            "V1EndpointSliceList",
        )


class FakeVersion:
    def get_code(self, **kw: Any) -> Any:
        return k8s(
            {
                "gitVersion": "v1.35.5+k3s1",
                "major": "1",
                "minor": "35",
                "buildDate": "",
                "compiler": "",
                "gitCommit": "",
                "gitTreeState": "",
                "goVersion": "",
                "platform": "",
            },
            "VersionInfo",
        )


@pytest.fixture
def core() -> FakeCore:
    return FakeCore()


@pytest.fixture
def tools(core: FakeCore, tmp_path: Path) -> K8sTools:
    apis = KubeApis(
        api=_api, core=core, apps=FakeApps(), discovery=FakeDiscovery(), version=FakeVersion()
    )  # type: ignore[arg-type]
    backend = K8sReadBackend(apis, clock=lambda: NOW)
    return K8sTools(
        backend, ToolRunner(AuditLog(tmp_path / "audit.jsonl", "opspilot-k8s", "live")), ["shop"]
    )


def call(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(fn(*args, **kwargs))
    return result


def test_list_pods(tools: K8sTools) -> None:
    pod = call(tools.list_pods, "shop")["pods"][0]
    assert pod == {
        "name": "payments-api-7c9d8b6f5-x2k4p",
        "phase": "Running",
        "ready": "0/1",
        "restarts": 6,
        "reason": "CrashLoopBackOff",
        "last_reason": "OOMKilled",
        "age": "30m",
        "node": "k3d-opspilot-server-0",
    }


def test_describe_pod_matches_condensed_shape(tools: K8sTools) -> None:
    d = call(tools.describe_pod, "shop", "payments-api-7c9d8b6f5-x2k4p")
    c = d["containers"][0]
    assert c["state"] == {"waiting": {"reason": "CrashLoopBackOff"}}
    assert c["lastState"] == {"terminated": {"reason": "OOMKilled", "exitCode": 137}}
    assert c["envNames"] == ["SERVICE_NAME", "MEMORY_BALLAST_MB"]
    assert "300" not in json.dumps(c)  # env values never leave the server
    assert c["probes"]["readiness"].startswith("GET /readyz :http every 5s")
    assert d["volumes"] == ["data (empty_dir)"]
    assert [e["reason"] for e in d["events"]] == ["BackOff", "Pulled"]  # newest first, old dropped
    assert "pw123456" not in json.dumps(d)  # redacted
    assert d["truncated"] is False


def test_describe_missing_pod_is_clean_404(tools: K8sTools) -> None:
    err = call(tools.describe_pod, "shop", "nope")["error"]
    assert err["type"] == "not_found"


def test_get_pod_logs_tail_filter_and_redaction(tools: K8sTools, core: FakeCore) -> None:
    out = call(tools.get_pod_logs, "shop", "p", previous=True, tail_lines=3)
    assert out["line_count"] == 3
    assert "abc3xyz" not in json.dumps(out)
    assert core.calls[-1] == (
        "read_namespaced_pod_log",
        {"previous": True, "tail_lines": 3, "_request_timeout": 10, "_preload_content": False},
    )
    assert call(tools.get_pod_logs, "shop", "p", contains="error")["lines"] == ["ERROR boom"]


def test_get_events_window_and_selector(tools: K8sTools, core: FakeCore) -> None:
    out = call(
        tools.get_events,
        "shop",
        involved_object_name="payments-api-7c9d8b6f5-x2k4p",
        since_minutes=3,
    )
    assert [e["reason"] for e in out["events"]] == ["BackOff"]
    assert core.calls[-1][1]["field_selector"] == "involvedObject.name=payments-api-7c9d8b6f5-x2k4p"


def test_deployment_and_rollout_history(tools: K8sTools) -> None:
    d = call(tools.get_deployment, "shop", "payments-api")
    assert d["revision"] == "3"
    assert d["replicas"]["ready"] == 0
    assert d["strategy"] == "RollingUpdate (maxSurge=0, maxUnavailable=1)"
    assert d["template"]["containers"][0]["image"] == "opspilot-demo-svc:dev"
    h = call(tools.get_rollout_history, "shop", "payments-api")
    assert [(r["revision"], r["images"]) for r in h["revisions"]] == [
        (2, ["opspilot-demo-svc:prev"]),
        (3, ["opspilot-demo-svc:dev"]),
    ]  # foreign RS excluded


def test_services_endpoints_configmap_nodes_pvcs(tools: K8sTools) -> None:
    assert call(tools.list_services, "shop")["services"][0]["ports"] == ["redis:6379->redis/TCP"]
    ep = call(tools.get_service_endpoints, "shop", "redis")
    assert (ep["ready"], ep["not_ready"], ep["addresses"][0]["pod"]) == (0, 1, "redis-1")
    cm = call(tools.get_configmap, "shop", "shopfront-settings")
    assert "hunter2xx" not in json.dumps(cm)
    assert cm["data"]["BIG"].endswith("…")
    node = call(tools.list_nodes)["nodes"][0]
    assert node["taints"] == ["dedicated=batch:NoSchedule"]
    pvc = call(tools.list_pvcs, "shop")["pvcs"][0]
    assert (pvc["status"], pvc["storage_class"], pvc["requested"]) == ("Pending", "gp3", "1Gi")
    assert tools.backend.configmap_names("shop") == ["a", "b"]  # type: ignore[attr-defined]
    assert tools.backend.server_version() == "v1.35.5+k3s1"  # type: ignore[attr-defined]


def test_namespace_allowlist_applies_to_every_namespaced_tool(tools: K8sTools) -> None:
    for fn in (tools.list_pods, tools.list_services, tools.list_pvcs):
        assert call(fn, "kube-system")["error"]["type"] == "namespace_not_allowed"
    assert call(tools.get_configmap, "kube-system", "x")["error"]["type"] == "namespace_not_allowed"


def test_every_call_is_audited(tools: K8sTools, tmp_path: Path) -> None:
    tools.list_pods("shop")
    tools.list_pods("kube-system")
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [(r["tool"], r["status"]) for r in records] == [
        ("list_pods", "ok"),
        ("list_pods", "rejected"),
    ]


def test_server_exposes_exactly_the_read_catalog(tools: K8sTools) -> None:
    listed = asyncio.run(build_server(tools).list_tools())
    names = {t.name for t in listed}
    assert names == set(DESCRIPTIONS)
    assert len(names) == 11
    assert not any(
        word in n
        for n in names
        for word in ("delete", "patch", "create", "exec", "scale", "secret")
    )
    for t in listed:
        assert len((t.description or "").split()) <= 80
        assert "Example:" in (t.description or "")


def test_server_call_through_mcp(tools: K8sTools) -> None:
    content = asyncio.run(build_server(tools).call_tool("list_pods", {"namespace": "shop"}))
    blocks = content if isinstance(content, list) else content[0]
    assert "payments-api" in blocks[0].text


def test_exec_and_tcp_probes_are_described() -> None:
    from opspilot.mcp_servers.k8s_readonly.backend import _probe

    exec_probe = k8s({"exec": {"command": ["redis-cli", "ping"]}, "periodSeconds": 5}, "V1Probe")
    tcp_probe = k8s({"tcpSocket": {"port": "redis"}}, "V1Probe")
    assert _probe(exec_probe) == "exec redis-cli ping every 5s, timeout 1s, fail after 3"
    assert _probe(tcp_probe).startswith("TCP :redis")  # type: ignore[union-attr]


def test_human_age() -> None:
    assert [human_age(s) for s in (5, 125, 7300, 200000)] == ["5s", "2m", "2h", "2d"]


def test_out_of_range_arguments_are_rejected_and_audited(tools: K8sTools, tmp_path: Path) -> None:
    assert (
        call(tools.get_pod_logs, "shop", "p", tail_lines=500)["error"]["type"] == "invalid_argument"
    )
    assert call(tools.get_events, "shop", since_minutes=0)["error"]["type"] == "invalid_argument"
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [r["status"] for r in records] == ["rejected", "rejected"]
