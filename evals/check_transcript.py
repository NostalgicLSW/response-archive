#!/usr/bin/env python3
"""Independently compare authored stdin in model tool calls to saved/fallback text.

Never executes transcript code. Unsupported command encodings are reported.
"""
import hashlib
import json
import re
import shlex
import sys
from pathlib import Path


def stdin_body(command):
    match = re.search(r"<<'([^']+)'[ \t]*\n", command)
    if match:
        return (command[match.end():].split("\n" + match.group(1), 1)[0] + "\n").encode()
    tokens = shlex.split(command)
    if tokens[:2] == ["printf", "%s"]:
        return tokens[2].encode()
    return None


def inspect(base):
    all_bodies = []
    unsupported = []
    for file in base.glob("session-*.json"):
        for item in json.loads(file.read_text())["events"]:
            payload = item.get("payload", {})
            if item.get("type") != "response_item" or payload.get("type") != "custom_tool_call":
                continue
            text = payload.get("input", "")
            match = re.search(r'(?:"cmd"|cmd)\s*:\s*("(?:\\.|[^"\\])*")', text)
            if not match:
                continue
            command = json.loads(match.group(1))
            if "archive.py save " not in command:
                continue
            body = stdin_body(command)
            if body is None:
                unsupported.append(payload.get("call_id"))
            else:
                all_bodies.append(body)
    files = list((base / "workspace/responses").rglob("*.md"))
    records = [{"path": str(p), "source_matches": sum(p.read_bytes() == body for body in all_bodies)} for p in files]
    report = json.loads((base / "verification-results.json").read_text())
    fallback = next(x["final"] for x in report["cases"] if x["label"] == "save-failure")
    failed_bodies = [body for body in all_bodies if b"RA_FAILURE_BODY_14" in body]
    checks = {"all_saved_files_match_authored_source": all(x["source_matches"] == 1 for x in records),
              "failure_exact_original_prefix_preserved": len(failed_bodies) == 1 and fallback.startswith(failed_bodies[0].decode()),
              "all_save_calls_understood": not unsupported}
    result = {"passed": all(checks.values()), "checks": checks, "records": records, "unsupported": unsupported,
              "authored_stdin_count": len(all_bodies)}
    (base / "strict-original-checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    result = inspect(Path(sys.argv[1]))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
