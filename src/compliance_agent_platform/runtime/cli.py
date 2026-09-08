"""Local driver for the screening agent (stand-in for the AgentCore Runtime).

    cap run    --alert examples/alert_true_match.json
    cap resume --thread <alert_id> --resolution true_match --analyst-id A123 \
               --rationale "Confirmed DOB and nationality match"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from compliance_agent_platform.runtime.contract import invoke


def _print(result: dict) -> None:
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _cmd_run(args: argparse.Namespace) -> int:
    alert = json.loads(Path(args.alert).read_text())
    result = invoke({"alert": alert})
    _print(result)
    return 0 if result["status"] in {"disposed", "escalated"} else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    resume = {
        "resolution": args.resolution,
        "analyst_id": args.analyst_id,
        "rationale": args.rationale,
    }
    result = invoke({"thread_id": args.thread, "resume": resume})
    _print(result)
    return 0 if result["status"] == "disposed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cap", description="Sanctions-screening agent runner")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Screen a new alert")
    run.add_argument("--alert", required=True, help="Path to an alert JSON file")
    run.set_defaults(func=_cmd_run)

    resume = sub.add_parser("resume", help="Supply an analyst decision for an escalated alert")
    resume.add_argument("--thread", required=True, help="thread_id (the alert_id)")
    resume.add_argument(
        "--resolution", required=True, choices=["clear", "true_match", "insufficient_data"]
    )
    resume.add_argument("--analyst-id", required=True, dest="analyst_id")
    resume.add_argument("--rationale", required=True)
    resume.set_defaults(func=_cmd_resume)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
