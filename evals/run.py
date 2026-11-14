#!/usr/bin/env python3
"""Record each evaluate -> fix -> reevaluate cycle; no automatic code rewriting."""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "evals/runs" / (stamp + "-" + args.label + "-" + uuid.uuid4().hex[:6])
    output.mkdir(parents=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    run = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT, env=env, capture_output=True, text=True)
    (output / "tests.txt").write_text(run.stdout + run.stderr)
    source = ROOT / "skills/response-archive/scripts/archive.py"
    result = {"label": args.label, "passed": run.returncode == 0, "returncode": run.returncode,
              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "log": str(output / "tests.txt")}
    (output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    if run.returncode:
        print((run.stdout + run.stderr)[-8000:])
    return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
