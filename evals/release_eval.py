#!/usr/bin/env python3
"""Exercise the actual distribution ZIP, independently of the source checkout."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="response-archive-release-eval-", dir=scratch))
    source, workspace, home = base / "배포 with spaces", base / "작업 with spaces", base / "home"
    for path in [source, workspace, home]:
        path.mkdir()
    package = ROOT / "dist/response-archive-0.1.1.zip"
    env = dict(os.environ, CODEX_HOME=str(home), PYTHONDONTWRITEBYTECODE="1",
               TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch))
    with zipfile.ZipFile(package) as z:
        assert not any("auth.json" in p or "/responses/" in p or "/runs/" in p for p in z.namelist())
        z.extractall(source)
    entry = source / "response-archive-0.1.1/response_archive.py"

    def run(*args, body=None, entrypoint=entry):
        process = subprocess.run([sys.executable, "-B", str(entrypoint), *args, "--root", str(workspace)],
                                 input=body, capture_output=True, env=env)
        assert process.returncode == 0, (process.stdout, process.stderr)
        return json.loads(process.stdout)

    run("install", "--timezone", "Asia/Seoul")
    body = "# 배포 파일 시험\n\n원문 **보존** `$(literal)`\n".encode()
    saved = run("save", "--slug", "배포시험", "--surface", "cli", "--request-id", "release-body", body=body)
    assert Path(saved["path"]).read_bytes() == body
    assert saved["display"] == saved["relative_path"]
    assert run("save", "--slug", "다른제목", "--surface", "gui", "--request-id", "release-body", body=body)["replayed"]
    assert run("doctor")["healthy"]
    run("disable")
    assert run("save", "--surface", "cli", body=b"off")["status"] == "disabled"
    run("enable")
    assert run("save", "--surface", "gui", "--request-id", "release-second", body=b"GUI body")["display"].startswith("[")
    run("git", "include")
    assert "/responses/" not in (workspace / ".gitignore").read_text()
    installed = workspace / ".agents/skills/response-archive/scripts/archive.py"
    assert run("uninstall", entrypoint=installed)["records_preserved"]
    assert Path(saved["path"]).read_bytes() == body
    assert len(list((workspace / "responses").rglob("*.md"))) == 2
    result = {"passed": True, "zip_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
              "root": str(base), "checks": ["unpacked install without dependencies", "Unicode and spaces",
              "verbatim", "retry", "per-call surface", "doctor", "off/on", "Git option",
              "installed tool self-removal", "records preserved", "no credentials or runtime logs"]}
    (base / "verification-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
