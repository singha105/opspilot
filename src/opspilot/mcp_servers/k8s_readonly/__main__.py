"""Entry point: ``opspilot-k8s`` or ``python -m opspilot.mcp_servers.k8s_readonly``."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from opspilot.config import get_settings
from opspilot.logging import configure_logging
from opspilot.mcp_servers.common import AuditLog, ToolRunner
from opspilot.mcp_servers.common.kube import apis_from_kubeconfig
from opspilot.mcp_servers.k8s_readonly.backend import K8sReadBackend
from opspilot.mcp_servers.k8s_readonly.server import SERVER_NAME, K8sTools, build_server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opspilot-k8s", description=__doc__)
    parser.add_argument(
        "--kubeconfig", help="Reader kubeconfig (default: .secrets/opspilot-reader.kubeconfig)"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)  # stderr; stdout is MCP
    kubeconfig = Path(args.kubeconfig) if args.kubeconfig else settings.reader_kubeconfig
    backend = K8sReadBackend(apis_from_kubeconfig(kubeconfig))
    runner = ToolRunner(AuditLog(settings.audit_log, SERVER_NAME, "live"))
    build_server(K8sTools(backend, runner, settings.allowed_namespaces)).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
