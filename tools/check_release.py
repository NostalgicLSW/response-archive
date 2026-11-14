#!/usr/bin/env python3
"""Fail-closed publication checks. Report paths/categories, never matched secrets."""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{25,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    "openai_token": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{35,}\b"),
    "personal_home_path": re.compile(rb"/Users/[A-Za-z0-9._-]+/(?:Documents|Desktop|\.codex)(?:/|\b)"),
}


def checked_path(root, relative):
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise ValueError("invalid_public_path")
    path = root
    for part in value.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink_in_public_path")
    return path


def public_files(root):
    manifest = checked_path(root, "release-files.txt")
    names = [line.strip() for line in manifest.read_text().splitlines() if line.strip() and not line.startswith("#")]
    if len(set(names)) != len(names) or not {"LICENSE", "README.md", "release-files.txt", "skills/response-archive/scripts/_vendor/TOMLI-LICENSE"}.issubset(names):
        raise ValueError("invalid_release_manifest")
    for name in names:
        if not checked_path(root, name).is_file():
            raise ValueError("missing_public_file: " + name)
    return names


def sensitive_name(name):
    p = Path(name)
    return (any(part in {"responses", ".response-archive", "__pycache__"} for part in p.parts)
            or p.parts[:2] == ("evals", "runs")
            or p.name in {"auth.json", "credentials.json", ".response-archive.json", "id_rsa", "id_ed25519"}
            or p.suffix in {".pem", ".key"}
            or (p.name.startswith(".env") and not p.name.endswith(".example")))


def content_findings(name, data, source="working_tree"):
    if sensitive_name(name):
        return [{"file": name, "reason": "private_or_runtime_path", "source": source}]
    return [{"file": name, "reason": label, "source": source}
            for label, pattern in PATTERNS.items() if pattern.search(data)]


def check(root=ROOT, tracked=False):
    names = public_files(root)
    candidates = set(names)
    indexed = set()
    if tracked:
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError("tracked_check_requires_git_repository")
        indexed = {value.decode("utf-8") for value in result.stdout.split(b"\0") if value}
        candidates.update(indexed)
    findings = []
    for name in sorted(indexed - set(names)):
        findings.append({"file": name, "reason": "not_in_public_manifest", "source": "git_index"})
    for name in sorted(candidates):
        try:
            path = checked_path(root, name)
            if not path.is_file():
                raise ValueError("missing_tracked_file")
            data = path.read_bytes()
            findings.extend(content_findings(name, data))
            if name in indexed:
                blob = subprocess.run(["git", "show", ":" + name], cwd=root, capture_output=True, timeout=10)
                if blob.returncode:
                    findings.append({"file": name, "reason": "unreadable_git_index"})
                else:
                    findings.extend(content_findings(name, blob.stdout, "git_index"))
                    if blob.stdout != data:
                        findings.append({"file": name, "reason": "index_worktree_difference"})
        except (ValueError, OSError) as error:
            findings.append({"file": name, "reason": type(error).__name__})
    return names, findings


def main():
    try:
        names, findings = check(tracked="--tracked" in sys.argv[1:])
        print(json.dumps({"passed": not findings, "public_files": len(names), "findings": findings}))
        return 1 if findings else 0
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"passed": False, "reason": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
