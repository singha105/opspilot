"""RBAC proofs: the reader is read-only and the operator can only patch Deployments in shop.

Needs: make cluster-up demo-build demo-deploy rbac-apply
"""

import subprocess
from collections.abc import Callable
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from opspilot.config import get_settings
from opspilot.kube import client_from_kubeconfig

pytestmark = pytest.mark.integration

SA_PREFIX = "system:serviceaccount:opspilot-system:"


def can_i(sa: str, verb: str, resource: str, namespace: str = "shop") -> bool:
    # "pods/exec" as a positional arg means "the pod named exec", so subresources
    # must go through --subresource to test what the row name says.
    base, _, subresource = resource.partition("/")
    extra = ["--subresource", subresource] if subresource else []
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            get_settings().admin_context,
            "auth",
            "can-i",
            verb,
            base,
            *extra,
            "-n",
            namespace,
            "--as",
            SA_PREFIX + sa,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() == "yes"


READER_ALLOWED = [
    ("get", "pods"),
    ("list", "pods"),
    ("watch", "pods"),
    ("get", "pods/log"),
    ("list", "events"),
    ("list", "deployments.apps"),
    ("list", "replicasets.apps"),
    ("list", "services"),
    ("list", "endpoints"),
    ("list", "endpointslices.discovery.k8s.io"),
    ("list", "configmaps"),
    ("list", "nodes"),
    ("list", "persistentvolumeclaims"),
    ("list", "namespaces"),
]
READER_DENIED = [
    ("get", "secrets"),
    ("list", "secrets"),
    ("create", "pods/exec"),
    ("get", "pods/exec"),
    ("delete", "pods"),
    ("create", "pods"),
    ("patch", "deployments.apps"),
    ("update", "deployments.apps"),
    ("patch", "deployments.apps/scale"),
    ("create", "configmaps"),
    ("delete", "namespaces"),
]
OPERATOR_ALLOWED = [
    ("get", "deployments.apps"),
    ("patch", "deployments.apps"),
    ("get", "deployments.apps/scale"),
    ("patch", "deployments.apps/scale"),
]
OPERATOR_DENIED = [
    ("list", "deployments.apps"),
    ("delete", "deployments.apps"),
    ("create", "deployments.apps"),
    ("update", "deployments.apps"),
    ("get", "secrets"),
    ("get", "pods"),
    ("delete", "pods"),
    ("create", "pods/exec"),
    ("patch", "configmaps"),
]


@pytest.mark.parametrize(("verb", "resource"), READER_ALLOWED)
def test_reader_allowed(verb: str, resource: str) -> None:
    assert can_i("opspilot-reader", verb, resource)


@pytest.mark.parametrize(("verb", "resource"), READER_DENIED)
def test_reader_denied(verb: str, resource: str) -> None:
    assert not can_i("opspilot-reader", verb, resource)


@pytest.mark.parametrize(("verb", "resource"), OPERATOR_ALLOWED)
def test_operator_allowed_in_shop(verb: str, resource: str) -> None:
    assert can_i("opspilot-operator", verb, resource)


@pytest.mark.parametrize(("verb", "resource"), OPERATOR_DENIED)
def test_operator_denied_in_shop(verb: str, resource: str) -> None:
    assert not can_i("opspilot-operator", verb, resource)


def test_operator_denied_outside_shop() -> None:
    assert not can_i("opspilot-operator", "patch", "deployments.apps", namespace="kube-system")


# ---- real API calls with the reader kubeconfig ------------------------------------


@pytest.fixture(scope="module")
def reader_api() -> client.ApiClient:
    return client_from_kubeconfig(get_settings().reader_kubeconfig)


def _assert_forbidden(call: Callable[[], Any]) -> None:
    with pytest.raises(ApiException) as info:
        call()
    assert info.value.status == 403


def test_reader_can_list_pods(reader_api: client.ApiClient) -> None:
    pods = client.CoreV1Api(reader_api).list_namespaced_pod("shop")
    assert {p.metadata.labels["app.kubernetes.io/name"] for p in pods.items} >= {
        "payments-api",
        "orders-api",
        "inventory-api",
        "redis",
    }


def test_reader_cannot_read_secrets(reader_api: client.ApiClient) -> None:
    _assert_forbidden(lambda: client.CoreV1Api(reader_api).list_namespaced_secret("shop"))


def test_reader_cannot_delete_pod(reader_api: client.ApiClient) -> None:
    core = client.CoreV1Api(reader_api)
    name = core.list_namespaced_pod("shop").items[0].metadata.name
    _assert_forbidden(lambda: core.delete_namespaced_pod(name, "shop"))


def test_reader_cannot_patch_deployment(reader_api: client.ApiClient) -> None:
    apps = client.AppsV1Api(reader_api)
    _assert_forbidden(
        lambda: apps.patch_namespaced_deployment("payments-api", "shop", {"spec": {"replicas": 2}})
    )


def test_reader_cannot_exec(reader_api: client.ApiClient) -> None:
    core = client.CoreV1Api(reader_api)
    name = core.list_namespaced_pod("shop").items[0].metadata.name
    _assert_forbidden(
        lambda: core.connect_get_namespaced_pod_exec(name, "shop", command=["id"], stdout=True)
    )
