"""Kubernetes clients for MCP servers: always from an explicit kubeconfig path."""

from dataclasses import dataclass
from pathlib import Path

from kubernetes import client

from opspilot.kube import client_from_kubeconfig


@dataclass(frozen=True)
class KubeApis:
    """Typed API groups built from one ApiClient."""

    api: client.ApiClient
    core: client.CoreV1Api
    apps: client.AppsV1Api
    discovery: client.DiscoveryV1Api
    version: client.VersionApi


def apis_from_kubeconfig(path: Path) -> KubeApis:
    """Build API groups from an explicit kubeconfig (reader or operator). Never the default."""
    api = client_from_kubeconfig(path)
    return KubeApis(
        api=api,
        core=client.CoreV1Api(api),
        apps=client.AppsV1Api(api),
        discovery=client.DiscoveryV1Api(api),
        version=client.VersionApi(api),
    )
