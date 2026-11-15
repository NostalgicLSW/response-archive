"""Adversarial lifecycle and publication contracts. Synthetic local fixtures only."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from test_archive import Workspace, REPO, SCRIPT, a


class LifecycleAttack(Workspace):
    def test_manifest_cannot_delete_an_outside_file(self):
        self.install()
        (self.root / "child").mkdir()
        outside = self.root.parent / "outside.txt"
        outside.write_bytes(b"UNRELATED USER FILE")
        p = self.root / a.STATE / "install.json"
        manifest = json.loads(p.read_text())
        manifest["blocks"][0] = {"path": "child/../../outside.txt", "inserted": "UNRELATED USER FILE", "created": True}
        p.write_text(json.dumps(manifest))
        with self.assertRaises(a.ArchiveError):
            a.uninstall(self.root)
        self.assertEqual(outside.read_bytes(), b"UNRELATED USER FILE")

    def test_partial_copy_never_exposes_an_incomplete_skill(self):
        def fail(src, dst, *args, **kwargs):
            path = Path(dst)
            path.mkdir(parents=True, exist_ok=True)
            (path / "SKILL.md").write_text("partial copy")
            raise OSError("simulated full disk during copy")
        with patch.object(a.shutil, "copytree", side_effect=fail):
            with self.assertRaises(OSError):
                self.install()
        self.assertFalse((self.root / ".agents/skills/response-archive").exists())
        self.assertFalse((self.root / a.CONFIG).exists())
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_failure_after_guide_publication_rolls_back(self):
        guide = self.root / "AGENTS.md"
        guide.write_bytes(b"KEEP EXACTLY")
        original = a.fsync_dir
        fired = []
        def fail_once(path):
            if path == self.root and a.BEGIN in guide.read_bytes() and not fired:
                fired.append(True)
                raise OSError("fsync failed after replace")
            return original(path)
        with patch.object(a, "fsync_dir", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(guide.read_bytes(), b"KEEP EXACTLY")
        self.install()
        self.assertTrue(a.doctor(self.root)["healthy"])

    def test_failure_after_manifest_publication_is_recoverable(self):
        original = a.atomic_write
        fired = []
        def fail_once(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name == "install.json" and not fired:
                fired.append(True)
                raise OSError("manifest published but acknowledgement failed")
            return result
        with patch.object(a, "atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.install()
        self.install()
        self.assertTrue(a.doctor(self.root)["healthy"])

    def test_killed_git_update_recovers_on_next_command(self):
        self.install()
        code = "import os\noriginal=a.atomic_write\ndef crash(path,*args,**kwargs):\n result=original(path,*args,**kwargs)\n if path.name=='.gitignore':os._exit(41)\n return result\na.atomic_write=crash\na.set_git(root,True)"
        child = self.child(code)
        self.assertEqual(child.returncode, 41, child.stderr)
        a.status(self.root)
        self.assertTrue(a.doctor(self.root)["healthy"])
        self.assertFalse(a.load_config(self.root)["git_tracking"])
        self.assertIn("/responses/", (self.root / ".gitignore").read_text())

    def test_failed_uninstall_leaves_a_usable_installation(self):
        self.install()
        self.save(b"keep original")
        original = Path.unlink
        fired = []
        def fail_once(path, *args, **kwargs):
            if path == self.root / a.CONFIG and not fired:
                fired.append(True)
                raise OSError("cannot remove setting")
            return original(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_once):
            with self.assertRaises(OSError):
                a.uninstall(self.root)
        self.assertTrue(a.doctor(self.root)["healthy"])
        self.assertTrue(a.status(self.root)["folder_enabled"])
        self.assertEqual(self.files()[0].read_bytes(), b"keep original")

    def test_uninstall_does_not_expose_private_records_to_git(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.install()
        self.save(b"PRIVATE ANSWER")
        a.uninstall(self.root)
        visible = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=self.root, capture_output=True, text=True, check=True).stdout
        self.assertNotIn("responses/", visible)
        self.assertNotIn(a.STATE + "/", visible)
        self.install()
        self.assertTrue(a.doctor(self.root)["healthy"])

    def test_special_root_produces_a_roundtrippable_file_link(self):
        special = self.root.with_name("프로젝트 #1 > [notes] ? draft")
        self.root.rename(special)
        self.root = special
        self.install()
        result = self.save(output_surface="gui")
        target = result["display"].split("](<", 1)[1][:-2]
        self.assertEqual(unquote(urlsplit(target).path), result["path"])
        self.assertNotIn(">", target)

    def test_corrupt_receipt_returns_json_error_not_traceback(self):
        self.install()
        receipts = self.root / a.STATE / "receipts"
        receipts.mkdir()
        (receipts / (a.digest(b"bad") + ".json")).write_text("[]")
        run = subprocess.run([sys.executable, "-B", str(SCRIPT), "save", "--root", str(self.root), "--surface", "cli", "--request-id", "bad"], input=b"BODY", capture_output=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)["status"], "error")
        self.assertNotIn(b"Traceback", run.stderr)
        self.assertEqual(self.files(), [])

    def test_incomplete_manifest_cannot_claim_healthy(self):
        self.install()
        p = self.root / a.STATE / "install.json"
        manifest = json.loads(p.read_text())
        manifest["blocks"] = []
        p.write_text(json.dumps(manifest))
        run = subprocess.run([sys.executable, "-B", str(SCRIPT), "doctor", "--root", str(self.root)], capture_output=True)
        result = json.loads(run.stdout)
        self.assertTrue(run.returncode != 0 or result.get("healthy") is False)

    def test_doctor_does_not_claim_health_with_corrupt_receipt(self):
        self.install()
        self.save(request_id="first")
        receipt = self.root / a.STATE / "receipts" / (a.digest(b"first") + ".json")
        receipt.write_text("[]")
        self.assertFalse(a.doctor(self.root)["healthy"])

    def test_killed_install_is_recovered_before_reinstall(self):
        (self.root / "AGENTS.md").write_bytes(b"USER GUIDE")
        code = "import os\noriginal=a.atomic_write\ndef crash(path,*args,**kwargs):\n result=original(path,*args,**kwargs)\n if path.name=='AGENTS.md':os._exit(42)\n return result\na.atomic_write=crash\na.install(root,'Asia/Seoul')"
        self.assertEqual(self.child(code).returncode, 42)
        self.install()
        self.assertTrue(a.doctor(self.root)["healthy"])
        guide = (self.root / "AGENTS.md").read_bytes()
        self.assertTrue(guide.startswith(b"USER GUIDE"))
        self.assertEqual(guide.count(a.BEGIN), 1)

    def test_killed_uninstall_restores_a_usable_installation(self):
        self.install()
        self.save(b"preserve through uninstall kill")
        code = "import os\noriginal=Path.unlink\ndef crash(path,*args,**kwargs):\n result=original(path,*args,**kwargs)\n if path==root/a.CONFIG:os._exit(44)\n return result\nPath.unlink=crash\na.uninstall(root)"
        self.assertEqual(self.child(code).returncode, 44)
        self.assertTrue(a.status(self.root)["folder_enabled"])
        self.assertTrue(a.doctor(self.root)["healthy"])
        self.assertEqual(self.files()[0].read_bytes(), b"preserve through uninstall kill")

    def test_recovery_preserves_external_edits_and_stops(self):
        self.install()
        code = "import os\noriginal=a.atomic_write\ndef crash(path,*args,**kwargs):\n result=original(path,*args,**kwargs)\n if path.name=='.gitignore':os._exit(41)\n return result\na.atomic_write=crash\na.set_git(root,True)"
        self.assertEqual(self.child(code).returncode, 41)
        path = self.root / ".gitignore"
        user_bytes = path.read_bytes() + b"\nUSER EDIT AFTER INTERRUPTION\n"
        path.write_bytes(user_bytes)
        with self.assertRaises(a.ArchiveError) as error:
            a.status(self.root)
        self.assertEqual(error.exception.code, "recovery_conflict")
        self.assertEqual(path.read_bytes(), user_bytes)

    def test_git_include_choice_survives_uninstall_and_reinstall(self):
        self.install(tracking=True)
        a.uninstall(self.root)
        self.install()
        self.assertTrue(a.load_config(self.root)["git_tracking"])

    def test_interrupted_install_journal_remains_private_before_root_ignore(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("AGENTS.md\n")
        (self.root / "AGENTS.md").write_text("Private existing guidance")
        code = "import os\noriginal=a.write_json\ndef crash(path,value):\n result=original(path,value)\n if path.name=='transaction.json':os._exit(45)\n return result\na.write_json=crash\na.install(root,'Asia/Seoul')"
        self.assertEqual(self.child(code).returncode, 45)
        visible = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=self.root, capture_output=True, text=True, check=True).stdout
        self.assertNotIn(a.STATE + "/", visible)
        self.install()
        self.assertTrue(a.doctor(self.root)["healthy"])


class PublicationAttack(unittest.TestCase):
    def setUp(self):
        scratch = REPO / ".tmp"
        scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="archive-publication-eval-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "source"
        def ignored(path, names):
            return {n for n in names if n in {"dist", "__pycache__", ".git", ".tmp"} or (Path(path).name == "evals" and n == "runs")}
        shutil.copytree(REPO, self.root, ignore=ignored)

    def build(self):
        return subprocess.run([sys.executable, "-B", str(self.root / "tools/package.py")], capture_output=True, text=True)

    def test_stray_private_file_in_docs_is_not_shipped(self):
        (self.root / "docs/private.env").write_text("PRIVATE_CANARY=not-for-release")
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.root / "dist/response-archive-0.1.1.zip") as z:
            self.assertFalse(any(n.endswith("private.env") for n in z.namelist()))

    def test_credential_pattern_in_public_source_blocks_build(self):
        token = "ghp_" + "A" * 36
        with (self.root / "README.md").open("a") as f:
            f.write("\n" + token + "\n")
        result = self.build()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_missing_license_blocks_distribution(self):
        (self.root / "LICENSE").unlink()
        self.assertNotEqual(self.build().returncode, 0)

    def test_failed_build_keeps_previous_zip_intact(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        package = self.root / "dist/response-archive-0.1.1.zip"
        before = package.read_bytes()
        readme = self.root / "README.md"
        outside = self.root.parent / "outside-readme"
        outside.write_bytes(readme.read_bytes())
        readme.unlink()
        readme.symlink_to(outside)
        self.assertNotEqual(self.build().returncode, 0)
        self.assertEqual(package.read_bytes(), before)

    def test_actions_are_immutable_and_checkout_does_not_persist_token(self):
        workflow = (self.root / ".github/workflows/check.yml").read_text()
        refs = re.findall(r"uses:\s*[^\s@]+@([^\s]+)", workflow)
        self.assertTrue(refs)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs))
        self.assertIn("persist-credentials: false", workflow)

    def test_tracked_secret_outside_zip_manifest_blocks_publication(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        secret = self.root / "docs/unlisted.txt"
        secret.write_text("sk-" + "S" * 48)
        subprocess.run(["git", "add", "docs/unlisted.txt"], cwd=self.root, check=True)
        result = subprocess.run([sys.executable, "-B", str(self.root / "tools/check_release.py"), "--tracked"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("docs/unlisted.txt", result.stdout)
        self.assertNotIn(secret.read_text(), result.stdout + result.stderr)

    def test_staged_secret_is_detected_even_if_working_copy_is_clean(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        secret = self.root / "README.md"
        token = "ghp_" + "B" * 36
        secret.write_text(token)
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        secret.write_text("clean working copy, index still contains secret")
        result = subprocess.run([sys.executable, "-B", str(self.root / "tools/check_release.py"), "--tracked"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("README.md", result.stdout)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_late_secret_after_precheck_cannot_enter_zip(self):
        code = "import sys;sys.path.insert(0," + repr(str(self.root / "tools")) + ");import package as p\noriginal=p.check\ndef late(root):\n result=original(root)\n (root/'README.md').write_text('ghp_'+'C'*36)\n return result\np.check=late\nraise SystemExit(p.main())"
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ghp_" + "C" * 36, result.stdout + result.stderr)

    def test_unlisted_plain_private_note_cannot_be_published(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        note = self.root / "docs/private-notes.txt"
        note.write_text("Personal meeting notes, no token-shaped content")
        subprocess.run(["git", "add", "docs/private-notes.txt"], cwd=self.root, check=True)
        result = subprocess.run([sys.executable, "-B", str(self.root / "tools/check_release.py"), "--tracked"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not_in_public_manifest", result.stdout)
