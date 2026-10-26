"""Behavioral tests in isolated work directories; never touches a real project."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
SCRIPT = REPOSITORY / "scripts/project_cleanup.py"
spec = importlib.util.spec_from_file_location("cleanup", SCRIPT)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, relative, content="completed work\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def repo(self):
        self.git("init")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")

    def prepare(self, paths, extra=None):
        selection = self.base / ("selection-" + str(len(list(self.base.glob('selection-*')))) + ".json")
        selection.write_text(json.dumps({"items": [{
            "path": p, "reason": "Validated obsolete artifact", "evidence": ["Fixture acceptance passed"],
            "reference_check": "Inspected fixture references; none", "confidence": "high",
        } for p in paths]}), encoding="utf-8")
        out = self.base / (selection.stem + "-plan.json")
        result = cleanup.prepare(types.SimpleNamespace(root=str(self.root), selection=str(selection),
                                    out=str(out), include_ext=extra or []))
        return out, result["approval_sha256"]

    def apply(self, plan, token):
        with contextlib.redirect_stdout(io.StringIO()):
            result = cleanup.apply(types.SimpleNamespace(plan=str(plan), approve=token))
        return Path(result["journal"])

    def test_round_trip_preserves_unicode_media_office_and_bytes(self):
        self.put("CLAUDE.MD", "keep policy")
        self.put("plans/завершено.md", "# Готово\n- [x] Test\n")
        for name in ("draft.pptx", "notes.docx", "preview.mp4", "image.png"):
            (self.root / name).write_bytes(bytes(range(256)) * 4)
        paths = ["plans/завершено.md", "draft.pptx", "notes.docx", "preview.mp4", "image.png"]
        original = {p: (self.root / p).read_bytes() for p in paths}
        plan, token = self.prepare(paths)
        journal = self.apply(plan, token)
        self.assertTrue(all(not (self.root / p).exists() for p in paths))
        self.assertEqual((self.root / "CLAUDE.MD").read_text(), "keep policy")
        restored = cleanup.restore(types.SimpleNamespace(journal=str(journal)))
        self.assertEqual(restored["restored"], len(paths))
        self.assertTrue(all((self.root / p).read_bytes() == original[p] for p in paths))
        self.assertEqual(len(list(journal.parent.glob("*.blob"))), len(paths))
        self.assertEqual(cleanup.restore(types.SimpleNamespace(journal=str(journal)))["already_present"], len(paths))

    def test_all_preflight_before_first_mutation(self):
        first = self.put("first.md")
        second = self.put("second.md")
        plan, token = self.prepare(["first.md", "second.md"])
        second.write_text("Changed after approval")
        with self.assertRaises(cleanup.Refusal):
            self.apply(plan, token)
        self.assertTrue(first.exists())
        self.assertFalse((self.root / cleanup.STATE).exists())

    def test_changed_plan_or_wrong_token_rejected(self):
        path = self.put("done.md")
        plan, token = self.prepare(["done.md"])
        with self.assertRaises(cleanup.Refusal):
            self.apply(plan, "0" * 64)
        plan.write_bytes(plan.read_bytes() + b"\n")
        with self.assertRaises(cleanup.Refusal):
            self.apply(plan, token)
        self.assertTrue(path.exists())

    def test_protected_and_outside_paths_refused(self):
        paths = ["AGENTS.md", "CLAUDE.MD", "nested/CLAUDE.local.md", "SKILL.md", "README.md", "README.ru.md", "MEMORY.md",
                 ".github/copilot-instructions.md", ".claude/rules/cleanup.md", "src/main.py"]
        for path in paths:
            self.put(path)
            with self.subTest(path=path), self.assertRaises(cleanup.Refusal):
                self.prepare([path])
        self.put("outside.md")
        for path in ["../outside.md", "/outside.md", "C:/outside.md", "nested/../outside.md", "*.md"]:
            with self.subTest(path=path), self.assertRaises(cleanup.Refusal):
                self.prepare([path])

    def test_duplicate_and_low_evidence_refused(self):
        self.put("done.md")
        with self.assertRaises(cleanup.Refusal):
            self.prepare(["done.md", "done.md"])
        selection = self.base / "bad.json"
        selection.write_text('{"items":[{"path":"done.md","confidence":"high"}]}')
        with self.assertRaises(cleanup.Refusal):
            cleanup.prepare(types.SimpleNamespace(root=str(self.root), selection=str(selection),
                            out=str(self.base / "bad-plan.json"), include_ext=[]))

    def test_git_dirty_tracked_refused_but_clean_can_round_trip(self):
        self.repo()
        path = self.put("done.md")
        self.git("add", ".")
        self.git("commit", "-m", "fixture")
        path.write_text("dirty")
        with self.assertRaises(cleanup.Refusal):
            self.prepare(["done.md"])
        self.git("restore", "done.md")
        plan, token = self.prepare(["done.md"])
        journal = self.apply(plan, token)
        cleanup.restore(types.SimpleNamespace(journal=str(journal)))
        self.assertEqual(self.git("diff", "--", "done.md"), "")

    def test_git_state_changes_and_nested_scope(self):
        self.repo()
        self.put("sub/done.md")
        self.git("add", ".")
        self.git("commit", "-m", "fixture")
        scope = self.root / "sub"
        snapshot = cleanup.git_snapshot(scope)
        self.assertEqual(cleanup.git_state(snapshot, "done.md"), {"tracked": True, "status": "clean"})
        plan, token = self.prepare(["sub/done.md"])
        self.git("rm", "--cached", "sub/done.md")
        with self.assertRaises(cleanup.Refusal):
            self.apply(plan, token)

    def test_nested_repository_is_boundary(self):
        self.put("child/note.md")
        (self.root / "child/.git").write_text("gitdir: elsewhere")
        with self.assertRaises(cleanup.Refusal):
            self.prepare(["child/note.md"])
        report = self.base / "inventory.json"
        cleanup.inventory(types.SimpleNamespace(root=str(self.root), out=str(report), include_ext=[]))
        self.assertIn("child", [i["path"] for i in json.loads(report.read_text())["skipped"]])

    def test_restore_conflicts_and_corruption_do_not_partially_restore(self):
        self.put("first.md")
        self.put("second.md")
        plan, token = self.prepare(["first.md", "second.md"])
        journal = self.apply(plan, token)
        self.put("second.md", "new important data")
        with self.assertRaises(cleanup.Refusal):
            cleanup.restore(types.SimpleNamespace(journal=str(journal)))
        self.assertFalse((self.root / "first.md").exists())
        (self.root / "second.md").unlink()
        (journal.parent / "000002.blob").write_text("corrupt")
        with self.assertRaises(cleanup.Refusal):
            cleanup.restore(types.SimpleNamespace(journal=str(journal)))
        self.assertFalse((self.root / "first.md").exists())

    def test_interrupted_apply_is_recoverable(self):
        self.put("first.md")
        self.put("second.md")
        plan, token = self.prepare(["first.md", "second.md"])
        original = cleanup.copy_exclusive
        calls = 0
        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated storage failure")
            original(source, destination)
        with patch.object(cleanup, "copy_exclusive", fail_second), self.assertRaises(OSError):
            self.apply(plan, token)
        journal = next((self.root / cleanup.STATE).rglob("journal.json"))
        result = cleanup.restore(types.SimpleNamespace(journal=str(journal)))
        self.assertEqual((result["restored"], result["already_present"]), (1, 1))
        self.assertTrue((self.root / "first.md").exists())

    def test_hardlink_and_symlink_refused_when_available(self):
        target = self.put("done.md")
        link = self.root / "hard.md"
        try:
            os.link(target, link)
        except OSError:
            self.skipTest("Hard links unavailable")
        with self.assertRaises(cleanup.Refusal):
            self.prepare(["hard.md"])
        link.unlink()
        sym = self.root / "sym.md"
        try:
            sym.symlink_to(target)
        except OSError:
            return
        with self.assertRaises(cleanup.Refusal):
            self.prepare(["sym.md"])

    def test_inventory_metadata_exclusions_and_no_overwrite(self):
        self.put("AGENTS.md")
        self.put("scratch.md")
        self.put("source.py")
        self.put("custom.myformat")
        self.put("node_modules/library/readme.md")
        out = self.base / "inventory.json"
        args = types.SimpleNamespace(root=str(self.root), out=str(out), include_ext=[".myformat"])
        cleanup.inventory(args)
        report = json.loads(out.read_text())
        self.assertEqual({x["path"] for x in report["files"]}, {"AGENTS.md", "scratch.md", "custom.myformat"})
        self.assertTrue(next(x for x in report["files"] if x["path"] == "AGENTS.md")["protected"])
        with self.assertRaises(FileExistsError):
            cleanup.inventory(args)

    def test_cli_requires_approval_and_inventory_works(self):
        target = self.put("note.md")
        out = self.base / "cli-inventory.json"
        process = subprocess.run([sys.executable, str(SCRIPT), "inventory", "--root", str(self.root),
                                  "--out", str(out)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["files"], 1)
        plan, token = self.prepare(["note.md"])
        process = subprocess.run([sys.executable, str(SCRIPT), "apply", "--plan", str(plan)],
                                 capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
