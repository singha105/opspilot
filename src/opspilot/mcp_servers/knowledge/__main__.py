"""Entry point: ``opspilot-kb`` or ``python -m opspilot.mcp_servers.knowledge``."""

import argparse
import sys
from collections.abc import Sequence

from opspilot.config import get_settings
from opspilot.logging import configure_logging
from opspilot.mcp_servers.common import AuditLog, ToolRunner
from opspilot.mcp_servers.knowledge.server import SERVER_NAME, KbTools, build_server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opspilot-kb", description=__doc__)
    parser.add_argument("--store", choices=["weaviate", "chroma", "pinecone"], default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)  # stderr; stdout is MCP

    def factory():  # type: ignore[no-untyped-def]
        from opspilot.rag.pipeline import build_retriever

        return build_retriever(args.store)

    runner = ToolRunner(AuditLog(settings.audit_log, SERVER_NAME, "live"))
    build_server(KbTools(factory, settings.knowledge_dir, runner)).run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
