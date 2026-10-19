"""Launch the optional local stdio MCP adapter against an existing database."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="Local advisor MCP over stdio. No HTTP port or voice-provider keys.")
    parser.add_argument("--db", required=True, help="Existing ServiceVoice SQLite database. No migration or seeding is performed.")
    parser.add_argument("--allow-status-updates", action="store_true", help="Enable local case-status writes for the trusted host.")
    parser.add_argument("--operator-label", help="Fixed unverified local label; required only when updates are enabled.")
    args = parser.parse_args()
    from runtime_sqlite import ensure_sqlite
    ensure_sqlite()
    try:
        from mcp.server.stdio import stdio_server
        from app.advisor_mcp import create_server
    except ImportError:
        parser.exit(2, "Install requirements-mcp-lock.txt with this Python interpreter first.\n")
    try:
        server = create_server(args.db, allow_status_updates=args.allow_status_updates, operator_label=args.operator_label)
    except ValueError as exc:
        parser.exit(2, str(exc) + "\n")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    async def serve():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
