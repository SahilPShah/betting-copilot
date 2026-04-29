#!/usr/bin/env python3
"""
Betting Copilot — Scheduled Job
================================
Replaces run_daily.sh + refresh_stats.py as a single Python entry point.

Modes:
  python scheduled_job.py                     Full run: ingest + predict + recommend (today)
  python scheduled_job.py --ingest-only       Morning run: ingest + refresh only (no ML)
  python scheduled_job.py --date 2026-04-28   Override target date for predict + recommend

Schedule (host cron on Droplet):
  TZ=America/New_York
  0 9  * * * docker exec betting-copilot-jobs python scheduled_job.py --ingest-only
  0 13 * * * docker exec betting-copilot-jobs python scheduled_job.py
"""

import argparse
import os
import subprocess
import sys
from datetime import date as date_cls, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


def _header(title: str) -> None:
    bar = "=" * 52
    print(f"\n{bar}")
    print(f"  {title}")
    print(f"{bar}")


def _run(label: str, *cmd: str) -> None:
    """Run a command, stream output, and exit on failure."""
    print(f"\n[{label}]")
    result = subprocess.run(list(cmd), cwd=BASE_DIR)
    if result.returncode != 0:
        print(f"\nERROR: '{label}' exited with code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)


def step_ingest() -> None:
    """
    Run the morning refresh: backfill missing game dates, fetch recent box scores
    (last 5 days to pick up final scores), capture today's odds, update team stats.
    This is the existing refresh_stats.py logic.
    """
    _run("1 — Ingest (backfill + odds + stats + boxscores)",
         PYTHON, os.path.join(BASE_DIR, "ingest", "refresh_stats.py"))


def step_predict(date_str: str) -> None:
    _run(f"2 — Predict  ({date_str})",
         PYTHON, os.path.join(BASE_DIR, "models", "predict.py"), "--date", date_str)


def step_recommend(date_str: str) -> None:
    _run(f"3 — Recommend  ({date_str})",
         PYTHON, os.path.join(BASE_DIR, "recs", "run_recs.py"), "--date", date_str)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Betting Copilot scheduled job (ingest → predict → recommend)."
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Target date for predict + recommend (default: today).",
    )
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="Run ingest/refresh only — skip predict and recommend. Used for the 9am morning job.",
    )
    args = parser.parse_args()

    date_str = args.date or date_cls.today().strftime("%Y-%m-%d")
    mode = "ingest-only" if args.ingest_only else "full"

    _header(
        f"Betting Copilot — Scheduled Job\n"
        f"  Date   : {date_str}\n"
        f"  Mode   : {mode}\n"
        f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    step_ingest()

    if args.ingest_only:
        _header(f"Ingest complete — {datetime.now().strftime('%H:%M:%S')}")
        return

    step_predict(date_str)
    step_recommend(date_str)

    _header(
        f"Full run complete\n"
        f"  Date    : {date_str}\n"
        f"  Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


if __name__ == "__main__":
    main()
