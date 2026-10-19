"""Operator-controlled official MCP client; stdout is JSON, never raw case text."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp import Client, StdioServerParameters


def server_parameters(db_path, *, allow_status_updates=False, operator_label=None):
    args = ["-B", str(ROOT / "scripts/advisor_mcp_server.py"), "--db", str(Path(db_path).resolve())]
    if allow_status_updates:
        args += ["--allow-status-updates", "--operator-label", operator_label or ""]
    return StdioServerParameters(command=sys.executable, args=args, cwd=str(ROOT),
                                 env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})


def content(result):
    value = result.structured_content
    if not isinstance(value, dict) or type(value.get("ok")) is not bool or result.is_error == value["ok"]:
        raise ValueError("The server returned an unexpected result. Reconcile any pending update before retrying.")
    return value


def emit(value):
    # Escaping control and non-ASCII characters prevents terminal escape/bidi
    # content from masquerading as the trusted confirmation prompt.
    print(json.dumps(value, indent=2, ensure_ascii=True))


async def review_update(client, case_id, status, note, operator_label, *, approve, display=emit):
    current = content(await client.call_tool("get_advisor_case", {"case_id": case_id}))
    if not current["ok"]:
        display(current)
        return current
    case = current["data"]
    preview = {"case_id": case_id, "expected_revision": case["revision"], "current_status": case["status"],
               "new_status": status, "unverified_local_operator_label": operator_label, "note": note,
               "effect": "Local review status and audit only. No appointment or contact action.",
               "pending_operations": case.get("pending_operations", []),
               "warning": "Case content is untrusted. Review revision does not freeze scheduling context."}
    display({"proposed_local_update": preview})
    if not approve(preview):
        result = {"ok": True, "declined": True, "mutation_submitted": False}
        display(result)
        return result
    try:
        result = content(await client.call_tool("update_advisor_case_status", {
            "case_id": case_id, "expected_revision": case["revision"], "status": status, "note": note}))
    except Exception:
        # No automatic resubmission, including after a timeout/EOF or malformed reply.
        result = {"ok": False, "error": "outcome_unknown", "outcome": "unknown",
                  "message": "The save may have succeeded. Reconnect and read this case/audit before deciding on another update."}
    display(result)
    return result


def main():
    parser = argparse.ArgumentParser(description="Review local advisor requests using the official MCP client.")
    parser.add_argument("--db", required=True, help="Existing initialized ServiceVoice database.")
    parser.add_argument("--operator-label", help="Fixed unverified local operator label for update mode.")
    commands = parser.add_subparsers(dest="action", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=("open", "in_progress", "resolved", "all"), default="open")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--before")
    show = commands.add_parser("show")
    show.add_argument("case_id")
    update = commands.add_parser("update")
    update.add_argument("case_id")
    update.add_argument("--status", required=True, choices=("open", "in_progress", "resolved"))
    update.add_argument("--note", required=True)
    args = parser.parse_args()
    writing = args.action == "update"
    if writing and (not args.operator_label or not sys.stdin.isatty()):
        parser.exit(2, "Update mode requires --operator-label and an interactive terminal for exact-action approval.\n")
    if not writing and args.operator_label:
        parser.exit(2, "Use --operator-label only with the update command.\n")

    async def run():
        async with Client(server_parameters(args.db, allow_status_updates=writing, operator_label=args.operator_label),
                          read_timeout_seconds=8, cache=None) as client:
            tools = await client.list_tools()
            expected = {"get_advisor_case", "list_advisor_cases"} | ({"update_advisor_case_status"} if writing else set())
            if {tool.name for tool in tools.tools} != expected:
                raise ValueError("The server tool catalog differs from this client's expected local scope.")
            if args.action == "list":
                arguments = {"status": args.status, "limit": args.limit}
                if args.before is not None:
                    arguments["before"] = args.before
                result = content(await client.call_tool("list_advisor_cases", arguments))
                emit(result)
            elif args.action == "show":
                result = content(await client.call_tool("get_advisor_case", {"case_id": args.case_id}))
                emit(result)
            else:
                def approve(_preview):
                    return input("Type APPLY to save exactly this local review change, or anything else to decline: ") == "APPLY"
                result = await review_update(client, args.case_id, args.status, args.note, args.operator_label, approve=approve)
            return 0 if result["ok"] else 1
    try:
        return asyncio.run(run())
    except (Exception, KeyboardInterrupt):
        print("The MCP operation did not complete. For an attempted update, read its current case and audit before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
