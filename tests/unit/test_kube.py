from pathlib import Path

import pytest

from opspilot.kube import KubeconfigMissingError, client_from_kubeconfig

KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
  - name: test
    cluster: {server: "https://127.0.0.1:6443", insecure-skip-tls-verify: true}
users:
  - name: reader
    user: {token: not-a-real-token}
contexts:
  - name: reader
    context: {cluster: test, user: reader}
current-context: reader
"""


def test_client_from_kubeconfig_uses_given_file(tmp_path: Path) -> None:
    path = tmp_path / "reader.kubeconfig"
    path.write_text(KUBECONFIG)
    api = client_from_kubeconfig(path)
    assert api.configuration.host == "https://127.0.0.1:6443"
    assert "Bearer not-a-real-token" in api.configuration.api_key.values()


def test_missing_kubeconfig_raises(tmp_path: Path) -> None:
    with pytest.raises(KubeconfigMissingError, match="make rbac-apply"):
        client_from_kubeconfig(tmp_path / "absent.kubeconfig")
