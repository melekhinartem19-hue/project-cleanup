#!/usr/bin/env python3
"""Inventory reviewed artifacts and quarantine an explicitly approved, pinned list.

Semantic classification and human approval belong to the invoking agent.
Python 3.10+, standard library only. No permanent deletion command.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
import uuid


EXTENSIONS = set((
    ".md .markdown .mdown .txt .rst .adoc .org .rtf .pdf .doc .docx .docm "
    ".odt .ppt .pptx .pptm .odp .xls .xlsx .xlsm .ods .csv .tsv "
    ".png .jpg .jpeg .gif .webp .avif .bmp .tif .tiff .svg .ico .heic "
    ".raw .cr2 .nef .psd .ai .eps .sketch .fig .drawio "
    ".mp4 .mov .mkv .avi .webm .m4v .mpg .mpeg .wav .mp3 .m4a .flac .ogg "
    ".zip .7z .rar .tar .gz .html .htm .ipynb .tex"
).split())
PROTECTED_NAMES = {
    "agents.md", "agents.override.md", "claude.md", "claude.local.md",
    "gemini.md", "qwen.md", "skill.md", "copilot-instructions.md", "memory.md",
    ".cursorrules", ".clinerules", ".windsurfrules", ".aider.conf.yml",
    ".aiderignore", ".gitignore", ".gitattributes", ".gitmodules",
    ".editorconfig", ".env", "license", "licence", "copying",
    "readme", "contributing", "code_of_conduct", "security", "changelog",
}
PROTECTED_DIRS = {
    ".git", ".hg", ".svn", ".claude", ".codex", ".agents", ".cursor",
    ".github", ".vscode", ".idea", ".windsurf", ".clinerules", ".roo",
    ".kilocode", ".continue", ".artifact-cleanup",
}
SKIP_DIRS = PROTECTED_DIRS | {
    "node_modules", "vendor", ".venv", "venv", "__pycache__", ".next",
    ".nuxt", ".cache", ".mypy_cache", ".pytest_cache", "dist", "build",
}
SOURCE_EXTENSIONS = set((
    ".py .js .mjs .cjs .ts .tsx .jsx .go .rs .java .cs .c .h .cpp .hpp "
    ".rb .php .swift .kt .sh .ps1 .bat .cmd .json .yaml .yml .toml .ini "
    ".xml .sql .css .scss .sass .less .vue .svelte .lock .sln .csproj"
).split())
STATE = ".artifact-cleanup"


class Refusal(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def linklike(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def root_path(value):
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise Refusal("Root must be a directory")
    return path


def scoped(root, relative):
    if not isinstance(relative, str) or not relative:
        raise Refusal("An explicit relative file path is required")
    rel = PurePosixPath(relative)
    if (rel.is_absolute() or "\\" in relative or ":" in relative
            or any(p in {"", ".", ".."} for p in relative.split("/"))
            or any(c in relative for c in "*?[]\x00")):
        raise Refusal(f"Unsafe relative path: {relative!r}")
    target = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if linklike(current):
                raise Refusal(f"Link or reparse point: {relative}")
    if not target.resolve().is_relative_to(root):
        raise Refusal(f"Path escapes root: {relative}")
    return target


def protection(relative):
    parts = PurePosixPath(relative).parts
    lower = [p.casefold() for p in parts]
    name = lower[-1]
    stem = Path(name).stem
    if any(p in PROTECTED_DIRS for p in lower[:-1]):
        return "agent, IDE, VCS or cleanup state directory"
    if (name in PROTECTED_NAMES or stem in PROTECTED_NAMES
            or name.endswith((".instructions.md", ".prompt.md", ".rules.md", ".mdc"))
            or name.startswith((".env.", "agents.", "claude.", "gemini.",
                                "readme.", "license.", "licence.", "license-", "licence-"))):
        return "instructions, policy, configuration or permanent project document"
    return None


def artifact(relative, extra):
    suffix = PurePosixPath(relative).suffix.casefold()
    return suffix in (EXTENSIONS | set(extra)) and suffix not in SOURCE_EXTENSIONS


def nested_repo(root, relative):
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        if any((current / marker).exists() for marker in (".git", ".hg", ".svn")):
            return True
    return False


def fingerprint(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or linklike(path) or before.st_nlink > 1:
        raise Refusal(f"Not an independent regular file: {path}")
    value = digest(path)
    after = path.lstat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise Refusal(f"File changed while hashing: {path}")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": value}


def git_snapshot(root):
    """Paths are relative to this scope, including when scope is below repo root."""
    def run(*args):
        process = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, timeout=60,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        if process.returncode:
            raise Refusal("Git metadata unavailable")
        return process.stdout.decode("utf-8", errors="surrogateescape")
    try:
        prefix = run("rev-parse", "--show-prefix").strip()
        tracked = set(filter(None, run("ls-files", "-z", "--cached", "--", ".").split("\0")))
        raw = run("status", "--porcelain=v1", "-z", "--untracked-files=all",
                  "--ignored=matching", "--", ".").split("\0")
        statuses = {}
        index = 0
        while index < len(raw):
            record = raw[index]
            index += 1
            if not record:
                continue
            status_code, path = record[:2], record[3:]
            paths = [path]
            if "R" in status_code or "C" in status_code:
                if index < len(raw):
                    paths.append(raw[index])
                index += 1
            for path in paths:
                if prefix and not path.startswith(prefix):
                    continue
                statuses[path[len(prefix):]] = status_code
        return {"available": True, "tracked": tracked, "statuses": statuses}
    except (OSError, subprocess.SubprocessError, Refusal):
        return {"available": False, "tracked": set(), "statuses": {}}


def git_state(snapshot, relative):
    if not snapshot["available"]:
        return {"tracked": None, "status": "unknown"}
    status_code = snapshot["statuses"].get(relative)
    if status_code is None:
        for path, code in snapshot["statuses"].items():
            if path.endswith("/") and relative.startswith(path):
                status_code = code
                break
    tracked = relative in snapshot["tracked"]
    return {"tracked": tracked, "status": status_code or ("clean" if tracked else "??")}


def writable_new(path):
    """Never overwrite an existing report or follow a linked output parent."""
    path = Path(path).absolute()
    for parent in [path, *path.parents]:
        if (parent.exists() or parent.is_symlink()) and linklike(parent):
            raise Refusal(f"Linked output path: {parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_new(path, value):
    path = writable_new(path)
    data = (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(data).hexdigest()


def save_journal(path, value):
    temporary = path.with_name("journal-" + uuid.uuid4().hex + ".tmp")
    write_new(temporary, value)
    os.replace(temporary, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def inventory(args):
    root = root_path(args.root)
    snapshot = git_snapshot(root)
    records, skipped, errors = [], [], []
    def onerror(error):
        errors.append({"path": str(error.filename), "error": str(error)})
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
        parent = Path(current)
        keep = []
        for name in sorted(dirs):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            try:
                reason = None
                if linklike(path):
                    reason = "link or reparse point"
                elif name.casefold() in SKIP_DIRS:
                    reason = "protected or generated/dependency directory"
                elif any((path / m).exists() for m in (".git", ".hg", ".svn")):
                    reason = "nested repository"
                if reason:
                    skipped.append({"path": relative, "reason": reason})
                else:
                    keep.append(name)
            except OSError as error:
                onerror(error)
        dirs[:] = keep
        for name in sorted(files):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            try:
                info = path.lstat()
                if linklike(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    skipped.append({"path": relative, "reason": "link, hard link or special file"})
                    continue
                protected = protection(relative)
                if not protected and not artifact(relative, args.include_ext):
                    continue
                records.append({"path": relative, "size": info.st_size,
                                "mtime_ns": info.st_mtime_ns, "extension": path.suffix.lower(),
                                "protected": protected, "git": git_state(snapshot, relative)})
            except OSError as error:
                onerror(error)
    report = {"version": 1, "root": str(root), "created_utc": now(),
              "git_available": snapshot["available"], "files": records,
              "skipped": skipped, "errors": errors}
    write_new(args.out, report)
    return {"report": str(Path(args.out).absolute()), "files": len(records),
            "skipped": len(skipped), "errors": len(errors)}


def checked_file(root, relative, extra, snapshot):
    path = scoped(root, relative)
    reason = protection(relative)
    if reason or not artifact(relative, extra) or nested_repo(root, relative):
        raise Refusal(f"Protected, unsupported or nested-repository path: {relative} ({reason})")
    git = git_state(snapshot, relative)
    if git["tracked"] and git["status"] != "clean":
        raise Refusal(f"Tracked file has local/index changes: {relative}")
    return path, git


def prepare(args):
    root = root_path(args.root)
    selection = read_json(args.selection)
    snapshot = git_snapshot(root)
    items, seen = [], set()
    if not isinstance(selection.get("items"), list) or not selection["items"]:
        raise Refusal("Selection must have a nonempty items list")
    for item in selection["items"]:
        relative = item.get("path")
        path, git = checked_file(root, relative, args.include_ext, snapshot)
        key = os.path.normcase(str(path))
        if key in seen:
            raise Refusal(f"Duplicate path: {relative}")
        seen.add(key)
        if (item.get("confidence") != "high" or not isinstance(item.get("reason"), str)
                or not item["reason"].strip() or not isinstance(item.get("reference_check"), str)
                or not item["reference_check"].strip() or not isinstance(item.get("evidence"), list)
                or not item["evidence"] or not all(isinstance(x, str) and x.strip() for x in item["evidence"])):
            raise Refusal(f"Missing high-confidence review evidence: {relative}")
        items.append({k: item[k] for k in ("path", "reason", "evidence", "reference_check", "confidence")}
                     | {"fingerprint": fingerprint(path), "git": git})
    plan = {"version": 1, "id": uuid.uuid4().hex, "root": str(root),
            "created_utc": now(), "operation": "quarantine", "include_ext": args.include_ext,
            "items": items}
    token = write_new(args.out, plan)
    return {"plan": str(Path(args.out).absolute()), "approval_sha256": token,
            "count": len(items), "bytes_to_quarantine": sum(i["fingerprint"]["size"] for i in items),
            "paths": [i["path"] for i in items]}


def validate_plan(plan):
    if (plan.get("version") != 1 or plan.get("operation") != "quarantine"
            or not re.fullmatch(r"[0-9a-f]{32}", str(plan.get("id", "")))
            or not isinstance(plan.get("items"), list) or not plan["items"]):
        raise Refusal("Unsupported or malformed plan")
    root = root_path(plan["root"])
    if str(root) != plan["root"]:
        raise Refusal("Project root moved or changed")
    return root


def copy_exclusive(source, destination):
    with source.open("rb") as original, destination.open("xb") as copied:
        shutil.copyfileobj(original, copied, 1024 * 1024)
        copied.flush()
        os.fsync(copied.fileno())
    shutil.copystat(source, destination, follow_symlinks=False)


def apply(args):
    raw = Path(args.plan).read_bytes()
    token = hashlib.sha256(raw).hexdigest()
    if args.approve != token:
        raise Refusal("Approval SHA-256 does not match this exact plan")
    plan = json.loads(raw)
    root = validate_plan(plan)
    snapshot = git_snapshot(root)
    seen = set()
    for item in plan["items"]:
        path, git = checked_file(root, item["path"], plan.get("include_ext", []), snapshot)
        key = os.path.normcase(str(path))
        if key in seen:
            raise Refusal("Duplicate plan path")
        seen.add(key)
        if fingerprint(path) != item["fingerprint"] or git != item["git"]:
            raise Refusal(f"File or Git state changed after review: {item['path']}")
    directory = scoped(root, f"{STATE}/quarantine/{plan['id']}")
    directory.mkdir(parents=True, exist_ok=False)
    journal_path = directory / "journal.json"
    journal = {"version": 1, "root": str(root), "id": plan["id"],
               "created_utc": now(), "approved_plan_sha256": token,
               "include_ext": plan.get("include_ext", []), "items": [
                   {"path": item["path"], "fingerprint": item["fingerprint"],
                    "blob": f"{index:06d}.blob", "state": "pending"}
                   for index, item in enumerate(plan["items"], 1)]}
    save_journal(journal_path, journal)
    print(json.dumps({"journal": str(journal_path)}, ensure_ascii=True), flush=True)
    for entry in journal["items"]:
        source = scoped(root, entry["path"])
        if fingerprint(source) != entry["fingerprint"]:
            raise Refusal(f"File changed before copy: {entry['path']}; journal: {journal_path}")
        blob = scoped(root, f"{STATE}/quarantine/{plan['id']}/{entry['blob']}")
        copy_exclusive(source, blob)
        if digest(blob) != entry["fingerprint"]["sha256"]:
            raise Refusal(f"Backup verification failed; original kept. Journal: {journal_path}")
        entry["state"] = "copied"
        save_journal(journal_path, journal)
        if fingerprint(source) != entry["fingerprint"]:
            raise Refusal(f"Source changed before removal; original kept. Journal: {journal_path}")
        source.unlink()
        entry["state"] = "quarantined"
        save_journal(journal_path, journal)
    return {"quarantined": len(journal["items"]), "journal": str(journal_path),
            "bytes_preserved": sum(i["fingerprint"]["size"] for i in journal["items"])}


def restore(args):
    supplied = Path(args.journal).absolute()
    journal = read_json(supplied)
    if journal.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{32}", str(journal.get("id", ""))):
        raise Refusal("Unsupported journal")
    root = root_path(journal["root"])
    expected = scoped(root, f"{STATE}/quarantine/{journal['id']}/journal.json")
    if str(root) != journal["root"] or supplied != expected:
        raise Refusal("Journal location or project root changed")
    operations, seen = [], set()
    for entry in journal["items"]:
        target = scoped(root, entry["path"])
        key = os.path.normcase(str(target))
        if key in seen or protection(entry["path"]) or nested_repo(root, entry["path"]):
            raise Refusal("Duplicate or protected restore target")
        seen.add(key)
        if not artifact(entry["path"], journal.get("include_ext", [])):
            raise Refusal("Unsupported restore target")
        if not re.fullmatch(r"[0-9]{6}\.blob", entry["blob"]):
            raise Refusal("Unsafe blob name")
        blob = scoped(root, f"{STATE}/quarantine/{journal['id']}/{entry['blob']}")
        wanted = entry["fingerprint"]["sha256"]
        if target.exists():
            if fingerprint(target)["sha256"] != wanted:
                raise Refusal(f"Restore would overwrite a different existing file: {entry['path']}")
            operations.append((entry, target, blob, False))
        else:
            if fingerprint(blob)["sha256"] != wanted:
                raise Refusal(f"Missing or corrupt backup: {entry['path']}")
            operations.append((entry, target, blob, True))
    restored, present = 0, 0
    for entry, target, blob, needed in operations:
        if needed:
            scoped(root, entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_exclusive(blob, target)
            if digest(target) != entry["fingerprint"]["sha256"]:
                raise Refusal(f"Restored copy verification failed: {entry['path']}")
            restored += 1
        else:
            present += 1
        entry["state"] = "restored"
        save_journal(expected, journal)
    return {"restored": restored, "already_present": present, "journal": str(expected),
            "backups_retained": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inventory", "prepare"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True)
        command.add_argument("--out", required=True)
        command.add_argument("--include-ext", action="append", default=[])
        if name == "prepare":
            command.add_argument("--selection", required=True)
    command = commands.add_parser("apply")
    command.add_argument("--plan", required=True)
    command.add_argument("--approve", required=True, help="SHA-256 of the plan actually approved by the user")
    command = commands.add_parser("restore")
    command.add_argument("--journal", required=True)
    args = parser.parse_args()
    if hasattr(args, "include_ext"):
        args.include_ext = [x.lower() if x.startswith(".") else "." + x.lower() for x in args.include_ext]
    try:
        result = {"inventory": inventory, "prepare": prepare, "apply": apply, "restore": restore}[args.command](args)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (Refusal, OSError, ValueError, KeyError, TypeError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
