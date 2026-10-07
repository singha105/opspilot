"""Entry point: ``opspilot-actions`` or ``python -m opspilot.mcp_servers.actions``."""

import argparse
import sys
from collections.abc import Sequence

from opspilot.agent.approval import NonceStore
from opspilot.config import get_settings
from opspilot.logging import configure_logging
from opspilot.mcp_servers.actions.executor import ActionExecutor
from opspilot.mcp_servers.actions.server import SERVER_NAME, ActionTools, build_server
from opspilot.mcp_servers.common import AuditLog, ToolRunner
from opspilot.mcp_servers.common.kube import apis_from_kubeconfig
from opspilot.mcp_servers.k8s_readonly.backend import K8sReadBackend


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opspilot-actions", description=__doc__)
    parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)  # stderr; stdout is MCP
    executor = ActionExecutor(
        operator=apis_from_kubeconfig(settings.operator_kubeconfig),
        reader=K8sReadBackend(apis_from_kubeconfig(settings.reader_kubeconfig)),
    )
    tools = ActionTools(
        executor,
        ToolRunner(AuditLog(settings.audit_log, SERVER_NAME, "live")),
        NonceStore(settings.nonce_db),
        settings.allowed_namespaces,
        settings,
    )
    build_server(tools).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
