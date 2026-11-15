#!/usr/bin/env python3
"""Response Archive: local, append-only Markdown answers. Python 3.9+."""
import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import tomllib
except ImportError:
    from _vendor import tomli as tomllib

VERSION = "0.1.0"
SKILL = "response-archive"
CONFIG = ".response-archive.json"
STATE = ".response-archive"
BEGIN = b"<!-- response-archive:start -->"
END = b"<!-- response-archive:end -->"
GIT_BEGIN = b"# response-archive:start"
GIT_END = b"# response-archive:end"


class ArchiveError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def digest(data):
    return hashlib.sha256(data).hexdigest()


def root_path(value):
    root = Path(value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ArchiveError("invalid_root", "작업 폴더가 아닙니다.")
    return root


def safe(root, *parts):
    """Never follow child symlinks into another archive or another project."""
    path = root
    for part in parts:
        fragment = Path(part)
        if fragment.is_absolute() or ".." in fragment.parts:
            raise ArchiveError("outside_root", "관리 경로에 절대경로나 상위 폴더 이동을 사용할 수 없습니다.")
        for component in fragment.parts:
            path = path / component
            if path.is_symlink():
                raise ArchiveError("symlink_path", "관리 경로에 심볼릭 링크가 있습니다: " + str(path))
    if root != path and root not in path.parents:
        raise ArchiveError("outside_root", "작업 폴더 밖에는 저장할 수 없습니다.")
    return path


def read_optional(path):
    return path.read_bytes() if path.exists() else None


def fsync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data, expected=None, check=False):
    if path.is_symlink():
        raise ArchiveError("symlink_path", "심볼릭 링크 파일은 변경하지 않습니다.")
    old = read_optional(path)
    if check and old != expected:
        raise ArchiveError("concurrent_edit", "파일이 다른 곳에서 변경됐습니다: " + str(path))
    mode = stat.S_IMODE(path.stat().st_mode) if old is not None else 0o600
    fd, name = tempfile.mkstemp(prefix=".archive-pending-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if check and read_optional(path) != expected:
            raise ArchiveError("concurrent_edit", "파일이 다른 곳에서 변경됐습니다: " + str(path))
        os.replace(name, str(path))
        fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


TX_PATHS = {"AGENTS.md", "AGENTS.override.md", ".gitignore", CONFIG,
            STATE + "/install.json", STATE + "/retained-git.json"}


def _decode_bytes(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 32 * 1024 * 1024:
        raise ArchiveError("invalid_journal", "복구 정보의 내용 형식이 올바르지 않습니다.")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        raise ArchiveError("invalid_journal", "복구 정보를 읽을 수 없습니다.")


def _apply_file(path, old, new):
    if read_optional(path) != old:
        raise ArchiveError("concurrent_edit", "파일이 다른 곳에서 변경됐습니다: " + str(path))
    if old == new:
        return
    if new is None:
        path.unlink()
        fsync_dir(path.parent)
    else:
        atomic_write(path, new, old, True)


def _directory_paths(root, directory):
    if not isinstance(directory, dict) or directory.get("action") not in ("install", "remove"):
        raise ArchiveError("invalid_journal", "설치 폴더 복구 정보가 올바르지 않습니다.")
    stage = directory.get("stage", "")
    if not isinstance(stage, str) or not re.fullmatch(r"\.(?:install|remove)-stage-[0-9a-f]{32}", stage):
        raise ArchiveError("invalid_journal", "임시 설치 경로가 올바르지 않습니다.")
    validate_files(directory.get("files"))
    return safe(root, ".agents", "skills", SKILL), safe(root, STATE, stage)


def _remove_owned_directory(path, expected, partial=False):
    if not path.exists():
        return
    actual = bundle_files(path)
    valid = (all(name in expected and expected[name] == value for name, value in actual.items())
             if partial else actual == expected)
    if not valid:
        raise ArchiveError("recovery_conflict", "설치 파일에 외부 변경이 있어 복구 과정에서 삭제하지 않습니다.")
    shutil.rmtree(path)


def recover(root):
    """Resume an interrupted lifecycle mutation before accepting another command."""
    journal_path = safe(root, STATE, "transaction.json")
    if not journal_path.exists():
        return
    value = json.loads(journal_path.read_text())
    if not isinstance(value, dict) or value.get("phase") not in ("pending", "committed") or not isinstance(value.get("ops"), list):
        raise ArchiveError("invalid_journal", "복구 정보의 형식이 올바르지 않습니다.")
    operations = []
    seen = set()
    for op in value["ops"]:
        if not isinstance(op, dict) or op.get("path") not in TX_PATHS or op["path"] in seen:
            raise ArchiveError("invalid_journal", "허용하지 않은 파일을 변경하는 복구 정보입니다.")
        seen.add(op["path"])
        operations.append((safe(root, op["path"]), _decode_bytes(op.get("before")), _decode_bytes(op.get("after"))))
    directory = value.get("directory")
    if directory is not None:
        target, stage = _directory_paths(root, directory)
    committed = value["phase"] == "committed"
    if committed:
        if any(read_optional(path) != after for path, before, after in operations):
            raise ArchiveError("recovery_conflict", "완료된 설정이 외부에서 변경됐습니다. 자동 복구하지 않습니다.")
    else:
        for path, before, after in reversed(operations):
            current = read_optional(path)
            if current == before:
                continue
            if current != after:
                raise ArchiveError("recovery_conflict", "복구 대상에 외부 변경이 있습니다: " + str(path))
            _apply_file(path, after, before)
    if directory is not None:
        expected = directory["files"]
        if directory["action"] == "install":
            if committed:
                if not target.exists() or bundle_files(target) != expected:
                    raise ArchiveError("recovery_conflict", "설치한 스킬이 변경됐습니다.")
            elif target.exists():
                if not any(target.iterdir()):
                    target.rmdir()
                else:
                    _remove_owned_directory(target, expected)
            _remove_owned_directory(stage, expected, partial=True)
        elif committed:
            if target.exists():
                raise ArchiveError("recovery_conflict", "제거 위치에 새로운 스킬이 있습니다. 보존합니다.")
            _remove_owned_directory(stage, expected, partial=True)
        elif stage.exists():
            if bundle_files(stage) != expected or target.exists():
                raise ArchiveError("recovery_conflict", "제거 중인 스킬에 외부 변경이 있습니다.")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(str(stage), str(target))
    journal_path.unlink()
    fsync_dir(journal_path.parent)


def transaction(root, changes, directory=None):
    operations = []
    for path, before, after in changes:
        relative = path.relative_to(root).as_posix()
        if relative not in TX_PATHS or read_optional(path) != before:
            raise ArchiveError("concurrent_edit", "설정 변경 전에 파일이 달라졌습니다: " + str(path))
        encode = lambda data: None if data is None else base64.b64encode(data).decode()
        operations.append({"path": relative, "before": encode(before), "after": encode(after)})
    journal = {"phase": "pending", "ops": operations, "directory": directory}
    journal_path = safe(root, STATE, "transaction.json")
    write_json(journal_path, journal)
    try:
        if directory:
            target, stage = _directory_paths(root, directory)
            target.parent.mkdir(parents=True, exist_ok=True)
            if directory["action"] == "install":
                target.mkdir()  # Exclusive reservation: never replace a preexisting folder.
                os.rename(str(stage), str(target))
            else:
                if bundle_files(target) != directory["files"]:
                    raise ArchiveError("skill_changed", "설치 파일이 변경돼 제거하지 않습니다.")
                os.rename(str(target), str(stage))
        for path, before, after in changes:
            _apply_file(path, before, after)
        write_json(journal_path, dict(journal, phase="committed"))
        recover(root)
    except BaseException:
        recover(root)
        raise


@contextlib.contextmanager
def locked(root, timeout=10.0, create=False):
    if not math.isfinite(timeout) or timeout < 0:
        raise ArchiveError("invalid_timeout", "대기 시간은 0 이상의 유한한 초 단위 숫자여야 합니다.")
    if fcntl is None:
        raise ArchiveError("unsupported_platform", "첫 버전은 macOS와 POSIX 로컬 파일시스템용입니다.")
    state = safe(root, STATE)
    if create:
        state.mkdir(mode=0o700, exist_ok=True)
    if not state.is_dir():
        raise ArchiveError("not_installed", "이 작업 폴더에는 응답 기록이 설치되지 않았습니다.")
    path = safe(root, STATE, "lock")
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ArchiveError("lock_timeout", "다른 저장 작업을 기다리는 시간이 초과됐습니다.")
                time.sleep(0.02)
        private_ignore = safe(root, STATE, ".gitignore")
        current_ignore = read_optional(private_ignore)
        if current_ignore is None:
            # Protect journals/backups even if installation stops before root rules exist.
            atomic_write(private_ignore, b"*\n", None, True)
        elif current_ignore != b"*\n":
            raise ArchiveError("private_state_unprotected", "내부 관리 폴더의 Git 보호 규칙이 변경됐습니다. 덮어쓰지 않습니다.")
        recover(root)
        yield state
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_config(root, allow_moved=False):
    path = safe(root, CONFIG)
    if not path.exists():
        raise ArchiveError("not_installed", "이 작업 폴더에는 응답 기록 설정이 없습니다.")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or cfg.get("version") != 1 or not isinstance(cfg.get("root"), str):
        raise ArchiveError("invalid_config", "설정 형식 또는 버전이 올바르지 않습니다.")
    if not allow_moved and cfg["root"] != str(root):
        raise ArchiveError("moved_root", "설치한 폴더의 위치가 바뀌었습니다. 현재 위치에서 uninstall 후 다시 install하세요. 기록은 보존됩니다.")
    if type(cfg.get("enabled")) is not bool or type(cfg.get("git_tracking")) is not bool:
        raise ArchiveError("invalid_config", "켜짐 상태와 Git 옵션은 true 또는 false여야 합니다.")
    if not isinstance(cfg.get("timezone"), str):
        raise ArchiveError("invalid_config", "설정에 시간대 정보가 없습니다.")
    timezone(cfg["timezone"])
    return cfg


def timezone(name):
    if name == "local":
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ArchiveError("invalid_timezone", "알 수 없는 시간대입니다: " + str(name))


def date_today(cfg):
    zone = timezone(cfg["timezone"])
    return (dt.datetime.now(zone) if zone else dt.datetime.now().astimezone()).date().isoformat()


def surface(value="auto", environ=None):
    env = os.environ if environ is None else environ
    value = env.get("RESPONSE_ARCHIVE_SURFACE", "auto") if value == "auto" else value
    if value in ("cli", "gui"):
        return value
    if value != "auto":
        raise ArchiveError("invalid_surface", "출력 환경은 cli 또는 gui여야 합니다.")
    # A TUI's tool calls inherit its terminal. Desktop tool calls normally use dumb.
    term = env.get("TERM", "").lower()
    origin = env.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "").lower()
    if term and term != "dumb":
        return "cli"
    if any(x in origin for x in ("desktop", "work", "gui")):
        return "gui"
    if "cli" in origin:
        return "cli"
    raise ArchiveError("surface_unknown", "실행 환경을 확인할 수 없습니다. 이 호출에 --surface cli 또는 gui를 지정하세요.")


def native_enabled(root):
    """Read explicit native overrides every time, including in cached conversations.

    The host's own skill discovery remains authoritative. No config is written.
    Named profiles, CLI -c overrides, and cloud policy are not reconstructed here.
    """
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    paths = [home / "config.toml", root / ".codex" / "config.toml"]
    installed = root / ".agents" / "skills" / SKILL
    candidates = {installed.resolve(), (installed / "SKILL.md").resolve(), Path(__file__).resolve().parent.parent}
    enabled = True
    for path in paths:
        if not path.is_file():
            continue
        with path.open("rb") as f:
            cfg = tomllib.load(f)
        skills = cfg.get("skills", {})
        if not isinstance(skills, dict) or not isinstance(skills.get("config", []), list):
            raise ArchiveError("invalid_native_config", "스킬 설정의 형식이 올바르지 않습니다.")
        for rule in skills.get("config", []):
            if not isinstance(rule, dict):
                raise ArchiveError("invalid_native_config", "스킬 활성화 규칙이 올바르지 않습니다.")
            name, rule_path = rule.get("name"), rule.get("path")
            matches = name == SKILL
            if rule_path:
                resolved = Path(rule_path).expanduser().resolve()
                matches = resolved in candidates or (resolved.name == "SKILL.md" and resolved.parent in candidates)
            if matches and type(rule.get("enabled")) is bool:
                enabled = rule["enabled"]
    return enabled


def state_result(root, cfg):
    native = native_enabled(root)
    return {"status": "enabled" if cfg["enabled"] and native else "disabled",
            "folder_enabled": cfg["enabled"], "native_enabled": native,
            "reason": None if cfg["enabled"] and native else ("native_disabled" if not native else "folder_disabled"),
            "root": str(root), "timezone": cfg["timezone"], "git_tracking": cfg["git_tracking"]}


def slugify(value):
    value = unicodedata.normalize("NFC", value)
    value = "".join(c if c.isalnum() or c in "_-" else "-" for c in value)
    return value.strip("-_")[:64] or "response"


def validate_receipt(value):
    if not isinstance(value, dict) or value.get("status") not in ("pending", "saved") or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", ""))):
        raise ArchiveError("invalid_receipt", "저장 위치 정보가 손상됐습니다. 기존 기록은 변경하지 않습니다.")
    parts = Path(value.get("path", "")).parts
    if len(parts) != 3 or parts[0] != "responses" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]) or not re.fullmatch(r"\d+_[\w-]+\.md", parts[2]):
        raise ArchiveError("invalid_receipt", "저장 위치 정보의 경로가 올바르지 않습니다.")
    return value


def saved_result(root, path, body_hash, request_id, output_surface, replay=False):
    rel = path.relative_to(root).as_posix()
    # Angle brackets preserve spaces in local paths. Filenames themselves are sanitized.
    gui = "[" + rel + "](<" + quote(str(path), safe="/") + ">)"
    return {"status": "saved", "path": str(path), "relative_path": rel,
            "sha256": body_hash, "request_id": request_id, "replayed": replay,
            "surface": output_surface, "display": rel if output_surface == "cli" else gui}


def save(root, body, title="response", request_id=None, output_surface="auto", timeout=10.0):
    if not body:
        raise ArchiveError("empty_answer", "저장할 답변이 비어 있습니다.")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        raise ArchiveError("invalid_encoding", "답변은 UTF-8이어야 합니다.")
    if len(body) > 32 * 1024 * 1024:
        raise ArchiveError("answer_too_large", "첫 버전은 답변 하나당 32 MiB까지 저장합니다.")
    rid = request_id or str(uuid.uuid4())
    if len(rid) > 256 or not rid.strip():
        raise ArchiveError("invalid_request_id", "재시도 식별자는 1~256자여야 합니다.")
    with locked(root, timeout):
        cfg = load_config(root)
        result = state_result(root, cfg)
        if result["status"] == "disabled":
            return result
        output_surface = surface(output_surface)
        receipts = safe(root, STATE, "receipts")
        receipts.mkdir(mode=0o700, exist_ok=True)
        receipt = safe(root, STATE, "receipts", digest(rid.encode()) + ".json")
        body_hash = digest(body)
        if receipt.exists():
            previous = validate_receipt(json.loads(receipt.read_text()))
            if previous.get("sha256") != body_hash:
                raise ArchiveError("request_reused", "같은 재시도 식별자에 다른 답변이 전달됐습니다.")
            old = previous.get("path", "")
            parts = Path(old).parts
            if len(parts) != 3 or parts[0] != "responses" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
                raise ArchiveError("invalid_receipt", "기록 위치 정보가 올바르지 않습니다.")
            path = safe(root, *parts)
            if path.exists():
                if digest(path.read_bytes()) != body_hash:
                    raise ArchiveError("record_changed", "이전에 저장한 기록이 외부에서 변경됐습니다. 덮어쓰지 않습니다.")
                return saved_result(root, path, body_hash, rid, output_surface, True)
            if previous.get("status") == "saved":
                raise ArchiveError("record_missing", "이전에 저장한 기록이 없어졌습니다. 자동으로 다시 만들지 않습니다.")
        date = date_today(cfg)  # Date is chosen after waiting for the shared lock.
        directory = safe(root, "responses", date)
        directory.mkdir(parents=True, exist_ok=True)
        numbers = [int(m.group(1)) for p in directory.iterdir()
                   for m in [re.match(r"^(\d+)_", p.name)] if m]
        # Reserve pending identities so a crash cannot let another answer take its path.
        for p in receipts.glob("*.json"):
            if p.is_symlink():
                raise ArchiveError("symlink_path", "기록 위치 정보에 심볼릭 링크가 있습니다.")
            pending = validate_receipt(json.loads(p.read_text()))
            parts = Path(pending.get("path", "")).parts
            if len(parts) == 3 and parts[1] == date:
                match = re.match(r"^(\d+)_", parts[2])
                if match:
                    numbers.append(int(match.group(1)))
        number = max(numbers, default=0) + 1
        target = directory / (str(number).zfill(3) + "_" + slugify(title) + ".md")
        intent = {"status": "pending", "sha256": body_hash, "path": target.relative_to(root).as_posix()}
        write_json(receipt, intent)
        # Write privately, fsync, then publish without replacing any existing file.
        fd, temporary = tempfile.mkstemp(prefix=".archive-pending-", dir=str(directory))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.link(temporary, str(target))
            fsync_dir(directory)
            write_json(receipt, dict(intent, status="saved"))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return saved_result(root, target, body_hash, rid, output_surface)


def bridge(root):
    return (BEGIN + b"\n" + ("Response Archive is configured for this exact session working directory: " + str(root) + ".\n"
        "For each final answer, use the available `response-archive` skill and its shared save tool, after checking the current folder setting. "
        "This also applies to ordinary questions, later turns and after compaction. Preserve explicit user instructions.\n"
        "Only apply to this exact working directory; do not inherit recording into a different child directory. "
        "If the host disables the skill or does not expose it, answer normally; never search for its files to bypass that state.\n"
        "Do not infer recording from chat memory. Check current settings on every turn. If saving fails, return the whole original answer and the actual reason.\n").encode() + END + b"\n")


def git_block(tracking):
    lines = ["/.response-archive/", "/.response-archive.json", "/.agents/skills/response-archive/"]
    if not tracking:
        lines.append("/responses/")
    return GIT_BEGIN + b"\n" + "\n".join(lines).encode() + b"\n" + GIT_END + b"\n"


def addition(path, block, begin, end):
    old = read_optional(path)
    data = old or b""
    if begin in data or end in data:
        raise ArchiveError("managed_block_conflict", "기존 관리 표시가 있어 자동으로 추가하지 않습니다: " + str(path))
    inserted = (b"\n" if data and not data.endswith(b"\n") else b"") + block
    return old, data + inserted, {"path": path.name, "inserted": inserted.decode(), "created": old is None}


def remove_block(root, record):
    path = safe(root, record["path"])
    data = read_optional(path)
    inserted = record["inserted"].encode()
    if data is None or data.count(inserted) != 1:
        raise ArchiveError("managed_block_changed", "제품이 추가한 지침이 변경됐습니다. 파일을 보존합니다: " + str(path))
    return path, data, data.replace(inserted, b"", 1)


def bundle_files(directory):
    result = {}
    for p in directory.rglob("*"):
        if "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        if p.is_symlink():
            raise ArchiveError("symlink_path", "설치 파일에 심볼릭 링크가 있어 변경하지 않습니다.")
        if p.is_file():
            result[p.relative_to(directory).as_posix()] = digest(p.read_bytes())
    return result


def validate_files(files):
    if not isinstance(files, dict) or not files or "SKILL.md" not in files or "scripts/archive.py" not in files:
        raise ArchiveError("invalid_manifest", "설치 파일 목록이 올바르지 않습니다.")
    for name, value in files.items():
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts or not re.fullmatch(r"[0-9a-f]{64}", str(value)):
            raise ArchiveError("invalid_manifest", "설치 파일 목록의 경로 또는 해시가 올바르지 않습니다.")


def validate_block(record):
    if not isinstance(record, dict) or record.get("path") not in ("AGENTS.md", "AGENTS.override.md", ".gitignore") or type(record.get("created")) is not bool or not isinstance(record.get("inserted"), str):
        raise ArchiveError("invalid_manifest", "관리 구역 정보가 올바르지 않습니다.")
    data = record["inserted"].encode()
    data = data[1:] if data.startswith(b"\n") else data
    if record["path"] == ".gitignore":
        valid = data in (git_block(False), git_block(True))
    else:
        valid = (data.startswith(BEGIN + b"\n") and data.endswith(END + b"\n") and
                 data.count(BEGIN) == 1 and data.count(END) == 1 and len(data) <= 128 * 1024)
    if not valid:
        raise ArchiveError("invalid_manifest", "관리 구역 경계가 손상됐습니다. 파일을 변경하지 않습니다.")
    return record


def load_manifest(root):
    value = json.loads(safe(root, STATE, "install.json").read_text())
    if not isinstance(value, dict) or not isinstance(value.get("blocks"), list) or len(value["blocks"]) != 2:
        raise ArchiveError("invalid_manifest", "설치 관리 정보가 손상됐습니다.")
    validate_files(value.get("files"))
    paths = [validate_block(record)["path"] for record in value["blocks"]]
    if paths.count(".gitignore") != 1 or len(set(paths)) != 2:
        raise ArchiveError("invalid_manifest", "설치 지침과 Git 관리 정보가 올바르지 않습니다.")
    return value


def install(root, zone="local", tracking=None):
    timezone(zone)
    package = Path(__file__).resolve().parent.parent
    destination = safe(root, ".agents", "skills", SKILL)
    cfg_path = safe(root, CONFIG)
    with locked(root, create=True) as state:
        manifest_path = safe(root, STATE, "install.json")
        if manifest_path.exists():
            load_manifest(root)
            return {"status": "already_installed", "root": str(root), "version": load_config(root)["version"]}
        if destination.exists() or cfg_path.exists():
            raise ArchiveError("install_conflict", "같은 이름의 기존 설정 또는 스킬이 있습니다. 덮어쓰지 않습니다.")
        # Codex gives a non-empty override precedence over AGENTS.md.
        override = safe(root, "AGENTS.override.md")
        guide = override if override.is_file() and override.stat().st_size else safe(root, "AGENTS.md")
        ignored = safe(root, ".gitignore")
        old_g, new_g, rec_g = addition(guide, bridge(root), BEGIN, END)
        retained_path = safe(root, STATE, "retained-git.json")
        old_retained = read_optional(retained_path)
        if old_retained is not None:
            retained = json.loads(old_retained)
            if not isinstance(retained, dict) or type(retained.get("git_tracking")) is not bool:
                raise ArchiveError("invalid_manifest", "보존한 Git 선택 정보가 손상됐습니다.")
            record = validate_block(retained.get("record"))
            if record["path"] != ".gitignore":
                raise ArchiveError("invalid_manifest", "보존할 Git 경로가 올바르지 않습니다.")
            path, old_i, _ = remove_block(root, record)
            tracking = retained["git_tracking"] if tracking is None else tracking
            rec_i = dict(record, inserted=("\n" if record["inserted"].startswith("\n") else "") + git_block(tracking).decode())
            new_i = old_i.replace(record["inserted"].encode(), rec_i["inserted"].encode(), 1)
        else:
            tracking = False if tracking is None else tracking
            old_i, new_i, rec_i = addition(ignored, git_block(tracking), GIT_BEGIN, GIT_END)
        cfg = {"version": 1, "root": str(root), "enabled": True, "timezone": zone, "git_tracking": tracking}
        stage = state / (".install-stage-" + uuid.uuid4().hex)
        try:
            shutil.copytree(package, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            files = bundle_files(stage)
            validate_files(files)
            manifest = {"version": VERSION, "files": files, "blocks": [rec_g, rec_i]}
            changes = [(guide, old_g, new_g), (ignored, old_i, new_i), (cfg_path, None, json_bytes(cfg)),
                       (manifest_path, None, json_bytes(manifest))]
            if old_retained is not None:
                changes.append((retained_path, old_retained, None))
            transaction(root, changes, {"action": "install", "stage": stage.name, "files": files})
        finally:
            if stage.exists() and not safe(root, STATE, "transaction.json").exists():
                shutil.rmtree(stage)
    return {"status": "installed", "version": VERSION, "root": str(root), "skill": str(destination / "SKILL.md"),
            "guide": guide.name, "timezone": zone, "git_tracking": tracking}


def set_enabled(root, enabled):
    with locked(root):
        cfg = load_config(root)
        cfg["enabled"] = enabled
        write_json(safe(root, CONFIG), cfg)
        return state_result(root, cfg)


def set_git(root, tracking):
    with locked(root) as state:
        cfg = load_config(root)
        manifest_path = safe(root, STATE, "install.json")
        manifest = load_manifest(root)
        old_manifest = manifest_path.read_bytes()
        cfg_path = safe(root, CONFIG)
        old_cfg = cfg_path.read_bytes()
        record = next(x for x in manifest["blocks"] if x["path"] == ".gitignore")
        path, data, without = remove_block(root, record)
        prefix = b"\n" if record["inserted"].startswith("\n") else b""
        new_inserted = prefix + git_block(tracking)
        new = data.replace(record["inserted"].encode(), new_inserted, 1)
        record["inserted"] = new_inserted.decode()
        cfg["git_tracking"] = tracking
        planned = [(path, data, new), (manifest_path, old_manifest, json_bytes(manifest)),
                   (cfg_path, old_cfg, json_bytes(cfg))]
        transaction(root, planned)
        return {"status": "configured", "git_tracking": tracking,
                "note": "제품의 제외 규칙만 변경했습니다. 기존 제외 규칙과 이미 추적 중인 파일은 그대로입니다."}


def uninstall(root):
    with locked(root) as state:
        manifest_path = safe(root, STATE, "install.json")
        manifest = load_manifest(root)
        destination = safe(root, ".agents", "skills", SKILL)
        if bundle_files(destination) != manifest["files"]:
            raise ArchiveError("skill_changed", "설치된 스킬에 변경되거나 추가된 파일이 있습니다. 삭제하지 않습니다.")
        # Preflight every owned block before changing anything.
        changes = [remove_block(root, record) + (record,) for record in manifest["blocks"]]
        # Ownership checks above cover the local files even after a directory move.
        # Saving still requires an exact root match; only removal permits relocation.
        cfg = load_config(root, allow_moved=True)
        planned = []
        for path, data, without, record in changes:
            if record["path"] != ".gitignore":
                planned.append((path, data, None if not without and record["created"] else without))
        git_record = next(record for record in manifest["blocks"] if record["path"] == ".gitignore")
        retained_path = safe(root, STATE, "retained-git.json")
        retained = {"record": git_record, "git_tracking": cfg["git_tracking"]}
        planned.extend([(safe(root, CONFIG), safe(root, CONFIG).read_bytes(), None),
                        (manifest_path, manifest_path.read_bytes(), None),
                        (retained_path, read_optional(retained_path), json_bytes(retained))])
        transaction(root, planned, {"action": "remove", "stage": ".remove-stage-" + uuid.uuid4().hex,
                                    "files": manifest["files"]})
        # Keep the lock inode: deleting it can split waiting and new writers.
        # Receipts keep retry identities across reinstallations. No answer is deleted.
    return {"status": "uninstalled", "records_preserved": True, "retry_metadata_preserved": True, "git_policy_preserved": True}


def status(root):
    with locked(root):
        return state_result(root, load_config(root))


def doctor(root):
    with locked(root):
        result = state_result(root, load_config(root))
        manifest = load_manifest(root)
        package = safe(root, ".agents", "skills", SKILL)
        checks = {"installed_files_unchanged": bundle_files(package) == manifest["files"]}
        for record in manifest["blocks"]:
            try:
                remove_block(root, record)
                checks[record["path"] + "_block_intact"] = True
            except ArchiveError:
                checks[record["path"] + "_block_intact"] = False
        checks["receipt_metadata_valid"] = True
        checks["saved_records_unchanged"] = True
        pending = 0
        receipts = safe(root, STATE, "receipts")
        for p in receipts.glob("*.json"):
            try:
                if p.is_symlink():
                    raise ArchiveError("invalid_receipt", "저장 위치 정보가 심볼릭 링크입니다.")
                value = validate_receipt(json.loads(p.read_text()))
                path = safe(root, *Path(value["path"]).parts)
                if value["status"] == "pending":
                    pending += 1
                if value["status"] == "saved" or path.exists():
                    checks["saved_records_unchanged"] &= path.is_file() and digest(path.read_bytes()) == value["sha256"]
            except (ArchiveError, OSError, ValueError, TypeError):
                checks["receipt_metadata_valid"] = False
        result.update(checks=checks, healthy=all(checks.values()), tool_version=VERSION, pending_retries=pending)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="작업 폴더의 에이전트 답변을 Markdown으로 기록합니다.")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "status", "enable", "disable", "save", "git", "doctor", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=".", help="설치한 정확한 작업 폴더")
        if name == "install":
            p.add_argument("--timezone", default="local", help="예: Asia/Seoul; 기본값은 실행 컴퓨터 현지 시간")
            p.add_argument("--git", choices=["include", "exclude"], default=None)
        if name == "save":
            p.add_argument("--slug", default="response")
            p.add_argument("--request-id")
            p.add_argument("--surface", choices=["auto", "cli", "gui"], default="auto")
            p.add_argument("--input", help="생략하면 표준입력의 UTF-8 원문을 읽습니다.")
            p.add_argument("--lock-timeout", type=float, default=10.0)
        if name == "git":
            p.add_argument("mode", choices=["include", "exclude"])
    args = parser.parse_args(argv)
    try:
        root = root_path(args.root)
        if args.command == "install":
            result = install(root, args.timezone, None if args.git is None else args.git == "include")
        elif args.command == "save":
            body = Path(args.input).read_bytes() if args.input else sys.stdin.buffer.read()
            result = save(root, body, args.slug, args.request_id, args.surface, args.lock_timeout)
        elif args.command in ("enable", "disable"):
            result = set_enabled(root, args.command == "enable")
        elif args.command == "git":
            result = set_git(root, args.mode == "include")
        else:
            result = {"status": status, "doctor": doctor, "uninstall": uninstall}[args.command](root)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ArchiveError, OSError, ValueError, KeyError, TypeError) as error:
        # The answer stays with the caller; never claim a successful save on error.
        print(json.dumps({"status": "error", "code": getattr(error, "code", type(error).__name__),
                          "reason": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
