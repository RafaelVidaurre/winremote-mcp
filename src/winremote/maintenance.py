"""Refuse service replacement while legacy workers could kill their workloads."""

import argparse
import json
from pathlib import Path

import psutil

from winremote.runs import Runs


def restart_blockers(runs: Runs, ancestors: set[int]) -> list[dict]:
    # Read only: a maintenance preflight must never repair or release claims.
    with runs.connect(write=False) as db:
        active = runs._active_rows(db)
    return [
        dict(id=run["id"], owner=run["owner"], state=run["state"])
        for run in active
        if run["kind"] == "run" and not run.get("metadata_outage_safe") and run.get("worker_pid") not in ancestors
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    blockers = restart_blockers(Runs(Path(args.root)), {p.pid for p in psutil.Process().parents()})
    print(json.dumps({"restart_safe": not blockers, "legacy_workers": blockers}))
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
