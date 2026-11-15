#!/usr/bin/env python3
"""Opt-in real interactive Codex evaluation. Never uses `codex exec`.

Uses a temporary CODEX_HOME, removes the auth copy in finally, and only gives
the model a disposable installed workspace. Original user files are read-only.
"""
import argparse
import datetime
import hashlib
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/response-archive/scripts"))
import archive


def rows(paths):
    result = []
    for path in paths:
        for line in path.read_text().splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return result


class TUI:
    def __init__(self, base, home, workspace, label):
        self.base, self.home, self.workspace, self.label = base, home, workspace, label
        self.closed = False
        self.before = set((home / "sessions").rglob("*.jsonl"))
        self.master, slave = pty.openpty()
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 120, 0, 0))
        env = dict(os.environ, CODEX_HOME=str(home), TERM="xterm-256color", COLORTERM="truecolor",
                   TMPDIR=str(base / "tmp"), XDG_CACHE_HOME=str(base / "cache"),
                   XDG_CONFIG_HOME=str(base / "xdg"), PYTHONDONTWRITEBYTECODE="1")
        self.process = subprocess.Popen(["codex", "--no-daemon", "--no-alt-screen", "-C", str(workspace),
                                         "-s", "workspace-write", "-a", "on-request"],
                                        stdin=slave, stdout=slave, stderr=slave, env=env, cwd=workspace,
                                        start_new_session=True)
        os.close(slave)
        self.log = (base / (label + ".terminal.raw")).open("wb", buffering=0)
        self.seen = bytearray()
        self.outcomes = []
        try:
            self.until(lambda: b"state=idle:app=codex" in self.seen, 90)
            self.pump(0.8)
        except BaseException:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.log.close()
            os.close(self.master)
            self.closed = True
            raise

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and self.process.poll() is None:
            ready, _, _ = select.select([self.master], [], [], min(.1, max(0, end - time.monotonic())))
            if not ready:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            self.log.write(data)
            self.seen.extend(data)
            for query, answer in [(b"\x1b[6n", b"\x1b[1;1R"), (b"\x1b[c", b"\x1b[?1;2c"),
                                   (b"\x1b[>c", b"\x1b[>0;276;0c"),
                                   (b"\x1b]10;?\x07", b"\x1b]10;rgb:ffff/ffff/ffff\x07"),
                                   (b"\x1b]11;?\x07", b"\x1b]11;rgb:0000/0000/0000\x07")]:
                if query in data:
                    os.write(self.master, answer)

    def until(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while not predicate():
            if self.process.poll() is not None:
                raise RuntimeError("Interactive CLI exited: " + self.seen[-1500:].decode(errors="replace"))
            if time.monotonic() > deadline:
                raise TimeoutError("Interactive CLI event timeout")
            self.pump(.2)

    def data(self):
        return rows(set((self.home / "sessions").rglob("*.jsonl")) - self.before)

    def send(self, prompt):
        os.write(self.master, ("\x1b[200~" + prompt + "\x1b[201~").encode())
        self.pump(.4)
        os.write(self.master, b"\r")
        self.pump(.4)

    def turn(self, label, prompt):
        count = lambda: sum(x.get("type") == "event_msg" and x.get("payload", {}).get("type") == "task_complete" for x in self.data())
        old = count()
        self.send(prompt)
        self.until(lambda: count() > old, 180)
        finals = [x["payload"] for x in self.data() if x.get("type") == "response_item" and x.get("payload", {}).get("phase") == "final_answer"]
        final = "\n".join(x.get("text", "") for x in finals[-1]["content"])
        snapshots = {p.relative_to(self.workspace).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (self.workspace / "responses").rglob("*.md")}
        result = {"label": label, "prompt": prompt, "final": final, "records": snapshots,
                  "folder_enabled": archive.load_config(self.workspace)["enabled"]}
        self.outcomes.append(result)
        print(json.dumps({"case": label, "count": len(snapshots), "final": final}, ensure_ascii=False), flush=True)
        self.pump(.3)
        self.persist()
        return result

    def compact(self):
        old = len(self.data())
        def done():
            for item in self.data()[old:]:
                if "compact" in item.get("type", "") or "compact" in item.get("payload", {}).get("type", ""):
                    return True
            return False
        self.send("/compact")
        self.until(done, 120)
        self.pump(1)
        self.persist()

    def persist(self):
        data = self.data()
        (self.base / (self.label + ".json")).write_text(json.dumps({"outcomes": self.outcomes,
            "session_ids": [x["payload"]["id"] for x in data if x.get("type") == "session_meta"],
            "events": data}, ensure_ascii=False, indent=2))

    def close(self):
        if self.closed:
            return
        try:
            if self.process.poll() is None:
                self.send("/quit")
                end = time.monotonic() + 10
                while self.process.poll() is None and time.monotonic() < end:
                    self.pump(.2)
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=10)
            try:
                self.persist()
            finally:
                self.log.close()
                os.close(self.master)
                self.closed = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-model", action="store_true", required=True, help="Opt in to real account/model use")
    args = parser.parse_args()
    scratch = ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="response-archive-native-eval-", dir=scratch))
    workspace, home = base / "workspace", base / "home"
    for p in [workspace, home, base / "tmp", base / "cache", base / "xdg"]:
        p.mkdir()
    guard_paths = [Path.home() / ".codex" / "config.toml", Path.home() / ".codex" / "auth.json"]
    guards = {str(p): {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "mtime_ns": p.stat().st_mtime_ns} for p in guard_paths if p.exists()}
    config = 'model_reasoning_effort="low"\ncli_auth_credentials_store="file"\ncheck_for_update_on_startup=false\nweb_search="disabled"\n[history]\npersistence="none"\n[features]\napps=false\nplugins=false\nmemories=false\nmulti_agent=false\nskill_search=false\n[tui]\nscreen_reader_detection_done=true\n'
    (home / "config.toml").write_text(config)
    archive.install(workspace, "Asia/Seoul")
    auth = home / "auth.json"
    report = {"root": str(base), "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "user_files_before": guards, "cases": [], "checks": {}}
    active = None
    old_home = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = str(home)
    report_path = base / "verification-results.json"
    print(json.dumps({"evaluation_root": str(base)}), flush=True)
    try:
        fd = os.open(str(auth), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write((Path.home() / ".codex/auth.json").read_bytes())
        active = TUI(base, home, workspace, "session-a")
        report["cases"].append(active.turn("ordinary", "업무 인수인계에서 결정 이유를 남기는 장점을 두 문장으로 설명하고 마지막에 RA_INITIAL_14 표식을 넣어줘."))
        report["cases"].append(active.turn("rich", "작업 기록의 장점을 제목, 작은 표, text 코드 블록으로 설명하고 마지막에 RA_RICH_14 표식을 넣어줘. 코드 블록에는 실행하지 않을 문자 그대로 $(example)과 `literal`을 넣어줘."))
        (home / "config.toml").write_text(config + '\n[[skills.config]]\nname="response-archive"\nenabled=false\n')
        report["cases"].append(active.turn("native-off-live", "앞서 설명한 장점을 한 문장으로 요약하고 RA_NATIVE_OFF_LIVE_14 표식을 넣어줘."))
        (home / "config.toml").write_text(config)
        report["cases"].append(active.turn("native-on-live", "다른 AI와 작업을 이어갈 때 기록이 도움이 되는 이유를 두 문장으로 설명하고 RA_NATIVE_ON_LIVE_14 표식을 넣어줘."))
        active.compact()
        report["checks"]["actual_context_compaction"] = True
        report["cases"].append(active.turn("after-compaction", "지금까지의 설명을 바탕으로 작업을 재개할 때 확인할 내용을 두 문장으로 정리하고 RA_AFTER_COMPACT_14 표식을 넣어줘."))
        report["cases"].append(active.turn("disable", "이 작업 폴더의 응답 기록을 꺼줘. 확인 문장에 RA_DISABLE_14 표식을 넣어줘."))
        report["cases"].append(active.turn("folder-off", "기록을 날짜별로 정리하는 장점을 한 문장으로 설명하고 RA_FOLDER_OFF_14 표식을 넣어줘."))
        active.close()
        active = TUI(base, home, workspace, "session-b")
        report["cases"].append(active.turn("fresh-off", "작업 기록이 인수인계에 도움이 되는 이유를 한 문장으로 설명하고 RA_FRESH_OFF_14 표식을 넣어줘."))
        report["cases"].append(active.turn("enable", "이 작업 폴더의 응답 기록을 켜줘. 확인 문장에 RA_ENABLE_14 표식을 넣어줘."))
        report["cases"].append(active.turn("fresh-on", "작업 결과와 남은 과제를 함께 기록하는 장점을 두 문장으로 설명하고 RA_FRESH_ON_14 표식을 넣어줘."))
        date_dir = workspace / "responses" / archive.date_today(archive.load_config(workspace))
        date_dir.chmod(0o555)
        try:
            report["cases"].append(active.turn("save-failure", "작업 이력의 장점을 제목, 두 문장, 작은 표로 설명하고 마지막에 RA_FAILURE_BODY_14 표식을 넣어줘."))
        finally:
            date_dir.chmod(0o700)
        active.close()
        (home / "config.toml").write_text(config + '\n[[skills.config]]\nname="response-archive"\nenabled=false\n')
        active = TUI(base, home, workspace, "session-c")
        report["cases"].append(active.turn("native-off-fresh", "결정 이유를 기록하는 장점을 한 문장으로 설명하고 RA_NATIVE_OFF_FRESH_14 표식을 넣어줘."))
        active.close()
        active = None
        cases = {x["label"]: x for x in report["cases"]}
        path_only = lambda x: re.fullmatch(r"responses/\d{4}-\d{2}-\d{2}/\d+_[^/]+\.md", x["final"]) is not None
        for label in ["ordinary", "rich", "native-on-live", "after-compaction", "fresh-on"]:
            report["checks"][label + "_saved_path_only"] = path_only(cases[label])
        for label, before in [("native-off-live", "rich"), ("disable", "after-compaction"), ("folder-off", "disable"), ("fresh-off", "folder-off"), ("enable", "fresh-off"), ("save-failure", "fresh-on"), ("native-off-fresh", "save-failure")]:
            report["checks"][label + "_no_record_changes"] = cases[label]["records"] == cases[before]["records"]
        for label in ["native-off-live", "folder-off", "fresh-off", "native-off-fresh"]:
            report["checks"][label + "_normal_answer"] = not path_only(cases[label]) and "RA_" in cases[label]["final"]
        failure = cases["save-failure"]["final"]
        report["checks"]["failure_full_body_and_reason"] = all(x in failure for x in ["RA_FAILURE_BODY_14", "|", "#"]) and any(x in failure for x in ["권한", "Permission", "Errno", "실패"])
        report["checks"]["exactly_five_saved_answers"] = len(cases["native-off-fresh"]["records"]) == 5
        report["passed"] = all(report["checks"].values())
    except Exception as error:
        report["passed"] = False
        report["infrastructure_or_behavior_error"] = {"type": type(error).__name__, "reason": str(error)}
    finally:
        if active is not None:
            try:
                active.close()
            except Exception as error:
                report["cleanup_error"] = str(error)
                report["passed"] = False
        auth.unlink(missing_ok=True)
        report["temporary_auth_removed"] = not auth.exists()
        report["user_files_after"] = {str(p): {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                                               "mtime_ns": p.stat().st_mtime_ns} for p in guard_paths if p.exists()}
        report["user_files_unchanged"] = all(report["user_files_after"].get(p) == before for p, before in guards.items())
        if not report["user_files_unchanged"]:
            report["environment_drift"] = "User config/auth comparison changed during the observation window. No restoration attempted; the evaluator writes only the temporary CODEX_HOME."
        if old_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old_home
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"passed": report.get("passed"), "report": str(report_path), "checks": report["checks"], "error": report.get("infrastructure_or_behavior_error")}, ensure_ascii=False), flush=True)
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
