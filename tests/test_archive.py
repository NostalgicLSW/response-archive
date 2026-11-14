"""Behavioral contracts; all writes occur in temporary workspaces."""
import concurrent.futures
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import unquote
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/response-archive/scripts/archive.py"
sys.path.insert(0, str(SCRIPT.parent))
import archive as a


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="response-archive-eval-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.root = Path(self.tmp.name) / "프로젝트 with spaces"
        self.root.mkdir()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def install(self, **kw):
        return a.install(self.root, "Asia/Seoul", **kw)

    def save(self, body=b"# Original\n\n**exact**\n", **kw):
        return a.save(self.root, body, output_surface=kw.pop("output_surface", "cli"), **kw)

    def files(self):
        return sorted((self.root / "responses").rglob("*.md"))

    def child(self, code):
        return subprocess.run([sys.executable, "-B", "-c", "import sys;sys.path.insert(0," + repr(str(SCRIPT.parent)) + ");import archive as a;from pathlib import Path;root=Path(" + repr(str(self.root)) + ");" + code], capture_output=True, text=True)


class Storage(Workspace):
    def setUp(self):
        super().setUp()
        self.install()

    def test_verbatim_unicode_crlf_no_final_newline(self):
        body = "# 한글\r\n\r\n|표|값|\r\n|---|---|\r\n|가|나|\r\n```sh\r\n$(not-executed) `literal`\r\n```".encode()
        result = self.save(body, title="제목 / ../탈출?")
        self.assertEqual(Path(result["path"]).read_bytes(), body)
        self.assertEqual(result["display"], result["relative_path"])
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(Path(result["path"]).parent.parent, self.root / "responses")

    def test_disabled_returns_normal_without_creating_response_directory(self):
        a.set_enabled(self.root, False)
        self.assertEqual(self.save()["status"], "disabled")
        self.assertFalse((self.root / "responses").exists())
        self.assertEqual(a.status(self.root)["status"], "disabled")

    def test_idempotent_retry_and_identity_collision(self):
        first = self.save(request_id="same-turn")
        second = self.save(request_id="same-turn", title="different-title")
        self.assertEqual(first["path"], second["path"])
        self.assertTrue(second["replayed"])
        with self.assertRaises(a.ArchiveError) as error:
            self.save(b"different", request_id="same-turn")
        self.assertEqual(error.exception.code, "request_reused")
        self.assertEqual(len(self.files()), 1)

    def test_user_modified_or_deleted_record_is_not_replaced(self):
        first = self.save(request_id="turn")
        path = Path(first["path"])
        path.write_bytes(b"user edit")
        with self.assertRaises(a.ArchiveError):
            self.save(request_id="turn")
        self.assertEqual(path.read_bytes(), b"user edit")
        path.unlink()
        with self.assertRaises(a.ArchiveError):
            self.save(request_id="turn")
        self.assertFalse(path.exists())

    def test_different_titles_share_one_sequence_and_existing_prefixes(self):
        directory = self.root / "responses" / a.date_today(a.load_config(self.root))
        directory.mkdir(parents=True)
        old = directory / "007_old.md"
        old.write_bytes(b"unchanged")
        first = self.save(title="alpha")
        second = self.save(title="beta")
        self.assertTrue(Path(first["path"]).name.startswith("008_"))
        self.assertTrue(Path(second["path"]).name.startswith("009_"))
        self.assertEqual(old.read_bytes(), b"unchanged")

    def test_lock_timeout_is_bounded(self):
        with a.locked(self.root):
            child = self.child("a.save(root,b'body',output_surface='cli',timeout=0.1)")
        self.assertNotEqual(child.returncode, 0)
        self.assertIn("ArchiveError", child.stderr)
        self.assertEqual(self.files(), [])

    def test_write_failure_never_publishes_partial_answer(self):
        with patch.object(a.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.save(b"body")
        self.assertEqual(self.files(), [])

    def test_exit_after_publication_recovers_same_file_on_retry(self):
        code = "import os;orig=a.write_json\ndef crash(path,value):\n if value.get('status')=='saved':os._exit(33)\n return orig(path,value)\na.write_json=crash\na.save(root,b'body',request_id='crash-after-link',output_surface='cli')"
        child = self.child(code)
        self.assertEqual(child.returncode, 33, child.stderr)
        self.assertEqual(len(self.files()), 1)
        result = self.save(b"body", request_id="crash-after-link")
        self.assertTrue(result["replayed"])
        self.assertEqual(len(self.files()), 1)

    def test_exit_before_publication_keeps_later_commit_order(self):
        child = self.child("import os;a.os.link=lambda *args:os._exit(32);a.save(root,b'first',request_id='crash-before-link',output_surface='cli')")
        self.assertEqual(child.returncode, 32, child.stderr)
        self.assertEqual(self.files(), [])
        second = self.save(b"second", title="beta", request_id="second")
        first = self.save(b"first", title="alpha", request_id="crash-before-link")
        self.assertLess(int(Path(second["path"]).name.split("_")[0]), int(Path(first["path"]).name.split("_")[0]))
        self.assertEqual(Path(first["path"]).read_bytes(), b"first")

    def test_symlinked_storage_cannot_write_outside_workspace(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.root / "responses").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(a.ArchiveError):
            self.save()
        self.assertEqual(list(outside.iterdir()), [])

    def test_saved_source_is_independent_of_output_surface(self):
        first = self.save(request_id="turn", output_surface="cli")
        second = self.save(request_id="turn", output_surface="gui")
        self.assertEqual(first["path"], second["path"])
        self.assertTrue(second["display"].startswith("[responses/"))
        self.assertIn("with spaces", unquote(second["display"]))
        self.assertEqual(len(self.files()), 1)

    def test_two_hundred_process_writes_no_duplicates_no_overwrite(self):
        code = "import json\nfor i in range(10):\n body=('worker-'+sys.argv[1]+'-'+str(i)).encode()\n a.save(root,body,title='description-'+sys.argv[1],request_id=sys.argv[1]+'-'+str(i),output_surface='cli')\n"
        pre = "import sys;sys.path.insert(0," + repr(str(SCRIPT.parent)) + ");import archive as a;from pathlib import Path;root=Path(" + repr(str(self.root)) + ");"
        workers = [subprocess.Popen([sys.executable, "-B", "-c", pre + code, str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(20)]
        try:
            for process in workers:
                out, err = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, err.decode())
        finally:
            for process in workers:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        files = self.files()
        self.assertEqual(len(files), 200)
        self.assertEqual([int(p.name.split("_")[0]) for p in files], list(range(1, 201)))
        self.assertEqual({p.read_bytes() for p in files}, {f"worker-{i}-{j}".encode() for i in range(20) for j in range(10)})
        receipts = [json.loads(p.read_text()) for p in (self.root / a.STATE / "receipts").glob("*.json")]
        self.assertEqual(len(receipts), 200)


class InstallAndSettings(Workspace):
    def test_install_remove_preserves_original_bytes_and_later_edits(self):
        originals = {"AGENTS.md": b"# Existing\r\nkeep this without final newline", ".gitignore": b"*.secret\n"}
        for name, data in originals.items():
            (self.root / name).write_bytes(data)
        self.install()
        original_lock = (self.root / a.STATE / "lock").stat().st_ino
        (self.root / "AGENTS.md").write_bytes((self.root / "AGENTS.md").read_bytes() + b"\nUser added this later.\n")
        saved = self.save()
        a.uninstall(self.root)
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), originals["AGENTS.md"] + b"\nUser added this later.\n")
        retained = json.loads((self.root / a.STATE / "retained-git.json").read_text())["record"]["inserted"].encode()
        self.assertEqual((self.root / ".gitignore").read_bytes().replace(retained, b"", 1), originals[".gitignore"])
        self.assertEqual(Path(saved["path"]).read_bytes(), b"# Original\n\n**exact**\n")
        self.assertEqual((self.root / a.STATE / "lock").stat().st_ino, original_lock)
        self.assertFalse((self.root / a.CONFIG).exists())

    def test_active_override_gets_bridge_without_changing_agents(self):
        (self.root / "AGENTS.md").write_text("base")
        (self.root / "AGENTS.override.md").write_text("override")
        self.install()
        self.assertEqual((self.root / "AGENTS.md").read_text(), "base")
        self.assertIn(a.BEGIN.decode(), (self.root / "AGENTS.override.md").read_text())
        a.uninstall(self.root)
        self.assertEqual((self.root / "AGENTS.override.md").read_text(), "override")

    def test_existing_skill_or_setting_not_overwritten(self):
        existing = self.root / a.CONFIG
        existing.write_bytes(b"user data")
        with self.assertRaises(a.ArchiveError):
            self.install()
        self.assertEqual(existing.read_bytes(), b"user data")
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_modified_installed_skill_prevents_destructive_remove(self):
        self.install()
        extra = self.root / ".agents/skills/response-archive/user.txt"
        extra.write_text("keep")
        with self.assertRaises(a.ArchiveError):
            a.uninstall(self.root)
        self.assertEqual(extra.read_text(), "keep")
        self.assertTrue((self.root / a.CONFIG).exists())

    def test_modified_managed_block_is_preserved(self):
        self.install()
        guide = self.root / "AGENTS.md"
        guide.write_text(guide.read_text().replace("For each final answer", "USER EDIT"))
        with self.assertRaises(a.ArchiveError):
            a.uninstall(self.root)
        self.assertIn("USER EDIT", guide.read_text())
        self.assertFalse(a.doctor(self.root)["healthy"])

    def test_git_option_and_external_ignore_rules(self):
        (self.root / ".gitignore").write_text("/responses/\n# user rule\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.install()
        a.set_git(self.root, True)
        self.assertTrue((self.root / ".gitignore").read_text().startswith("/responses/\n# user rule\n"))
        self.assertTrue(a.load_config(self.root)["git_tracking"])
        ignored = subprocess.run(["git", "check-ignore", "responses/example.md"], cwd=self.root, capture_output=True)
        self.assertEqual(ignored.returncode, 0)
        a.set_git(self.root, False)
        a.uninstall(self.root)
        retained = json.loads((self.root / a.STATE / "retained-git.json").read_text())["record"]["inserted"]
        self.assertEqual((self.root / ".gitignore").read_text().replace(retained, "", 1), "/responses/\n# user rule\n")

    def test_git_default_excludes_then_include_removes_only_owned_rule(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.install()
        check = lambda: subprocess.run(["git", "check-ignore", "responses/example.md"], cwd=self.root, capture_output=True).returncode
        self.assertEqual(check(), 0)
        a.set_git(self.root, True)
        self.assertEqual(check(), 1)

    def test_native_disable_is_read_on_every_call(self):
        self.install()
        path = self.home / "config.toml"
        path.write_text('[[skills.config]]\nname="response-archive"\nenabled=false\n')
        self.assertEqual(a.status(self.root)["reason"], "native_disabled")
        self.assertEqual(self.save()["status"], "disabled")
        self.assertEqual(self.files(), [])
        path.write_text('[[skills.config]]\nname="response-archive"\nenabled=true\n')
        self.assertEqual(self.save()["status"], "saved")

    def test_native_disable_by_path(self):
        self.install()
        path = self.root / ".agents/skills/response-archive/SKILL.md"
        (self.home / "config.toml").write_text('[[skills.config]]\npath=' + json.dumps(str(path)) + '\nenabled=false\n')
        self.assertFalse(a.status(self.root)["native_enabled"])

    def test_child_folder_does_not_inherit_root_config(self):
        self.install()
        child = self.root / "child"
        child.mkdir()
        with self.assertRaises(a.ArchiveError):
            a.status(child)

    def test_installer_rolls_back_on_write_error(self):
        (self.root / "AGENTS.md").write_bytes(b"original")
        original = a.atomic_write
        def fail(path, value, *args, **kw):
            if path.name == a.CONFIG:
                raise OSError("simulated full disk")
            return original(path, value, *args, **kw)
        with patch.object(a, "atomic_write", side_effect=fail):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), b"original")
        self.assertFalse((self.root / ".gitignore").exists())
        self.assertFalse((self.root / ".agents/skills/response-archive").exists())

    def test_moved_folder_can_remove_owned_install_and_reinstall(self):
        self.install()
        saved = self.save(b"preserve across move")
        relative = Path(saved["path"]).relative_to(self.root)
        moved = self.root.with_name("moved project")
        self.root.rename(moved)
        self.root = moved
        with self.assertRaises(a.ArchiveError):
            a.status(self.root)
        a.uninstall(self.root)
        self.assertEqual((moved / relative).read_bytes(), b"preserve across move")
        self.install()
        self.assertEqual(a.status(self.root)["status"], "enabled")

    def test_new_setting_created_during_install_is_not_overwritten(self):
        original = a.shutil.copytree
        def concurrent_setting(*args, **kw):
            result = original(*args, **kw)
            (self.root / a.CONFIG).write_bytes(b"user setting created concurrently")
            return result
        with patch.object(a.shutil, "copytree", side_effect=concurrent_setting):
            with self.assertRaises(a.ArchiveError):
                self.install()
        self.assertEqual((self.root / a.CONFIG).read_bytes(), b"user setting created concurrently")

    def test_git_option_rolls_back_if_setting_write_fails(self):
        self.install()
        paths = [self.root / ".gitignore", self.root / a.CONFIG, self.root / a.STATE / "install.json"]
        before = {p: p.read_bytes() for p in paths}
        original = a.atomic_write
        def fail(path, value, *args, **kwargs):
            if path.name == a.CONFIG:
                raise OSError("disk full when changing setting")
            return original(path, value, *args, **kwargs)
        with patch.object(a, "atomic_write", side_effect=fail):
            with self.assertRaises(OSError):
                a.set_git(self.root, True)
        self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_malformed_setting_returns_error_json_without_traceback(self):
        self.install()
        (self.root / a.CONFIG).write_text("[]")
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "status", "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "error")
        self.assertNotIn("Traceback", result.stderr)


class Surface(unittest.TestCase):
    def test_surface_is_per_call_and_not_persisted_in_folder(self):
        self.assertEqual(a.surface("auto", {"TERM": "xterm-256color", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}), "cli")
        self.assertEqual(a.surface("auto", {"TERM": "dumb", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}), "gui")
        self.assertEqual(a.surface("auto", {"TERM": "xterm", "RESPONSE_ARCHIVE_SURFACE": "gui"}), "gui")
        self.assertEqual(a.surface("cli", {}), "cli")
        with self.assertRaises(a.ArchiveError):
            a.surface("auto", {})


if __name__ == "__main__":
    unittest.main()
