"""Kubernetes API clients.

Agent code uses only the ServiceAccount kubeconfigs in ``.secrets/``. The admin
context is reserved for fault injection and is never the implicit default.
"""

from pathlib import Path

from kubernetes import client, config


class KubeconfigMissingError(FileNotFoundError):
    """Raised when a ServiceAccount kubeconfig has not been generated yet."""


def client_from_kubeconfig(path: Path) -> client.ApiClient:
    """Build an API client from an explicit kubeconfig file (reader or operator)."""
    if not path.is_file():
        raise KubeconfigMissingError(f"{path} not found; run 'make rbac-apply' to generate it")
    api: client.ApiClient = config.new_client_from_config(config_file=str(path))
    return api


def admin_client(context: str) -> client.ApiClient:
    """Build an API client for an explicitly named admin context (fault injection only)."""
    api: client.ApiClient = config.new_client_from_config(context=context)
    return api
