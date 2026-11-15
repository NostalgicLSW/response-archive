#!/usr/bin/env python3
"""Build an allowlisted ZIP; never include runtime responses, credentials or logs."""
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from check_release import check, checked_path, content_findings

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "response-archive-0.1.1"


def main():
    names, findings = check(ROOT)
    if findings:
        print(json.dumps({"passed": False, "findings": findings}))
        return 1
    # Scan the exact byte snapshot that will be written. Never re-read it later.
    contents = {name: checked_path(ROOT, name).read_bytes() for name in names}
    findings = [finding for name, data in contents.items() for finding in content_findings(name, data)]
    if findings:
        print(json.dumps({"passed": False, "findings": findings}))
        return 1
    checked_path(ROOT, "dist")
    output = checked_path(ROOT, "dist/" + PREFIX + ".zip")
    checksum = checked_path(ROOT, "dist/" + PREFIX + ".sha256")
    output.parent.mkdir(exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=".release-", dir=output.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name in sorted(names):
                info = zipfile.ZipInfo(PREFIX + "/" + name, (2026, 11, 14, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, contents[name])
        os.replace(staging, output)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)
    result = {"file": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "files": len(names)}
    checksum.write_text(result["sha256"] + "  " + output.name + "\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(json.dumps({"passed": False, "reason": str(error)}))
        raise SystemExit(1)
