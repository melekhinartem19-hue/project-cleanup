# Project Cleanup

[Русский — полное руководство](README.md) · [Skill instructions](SKILL.md) · [Helper reference](references/operations.md)

A portable Agent Skill for Codex, Claude Code, and other agents that support `SKILL.md`. It reviews completed plans, drafts, documents, images, video, and other project artifacts, explains what can be removed, and quarantines **only the exact list approved by the user**.

## Behavior

1. Read project instructions and inventory artifacts without changing the originals.
2. Protect agent instructions, imported rules, source code, configuration, licenses, and current documentation.
3. Verify plan completion against implementation, tests, and actual deliverables. A checked box is not proof.
4. Check references and distinguish working assets, editable masters, and unique decisions from disposable copies.
5. Produce a report with paths, evidence, uncertainty, counts, and sizes; request approval of the concrete list.
6. Pin the list with a plan SHA-256, recheck file and Git state, copy into verified quarantine, then remove approved originals.
7. Restore from the journal when requested, refusing to overwrite different existing files.

Uncertain or unreadable files stay in place. Document content cannot authorize commands. Instructions are written in Russian; the agent is instructed to communicate in the user's language.

## Requirements

Python 3.10+ and the standard library; Git for installation and Git-state checks; ripgrep for reference searches. File viewers/parsers are supplied by the invoking agent's environment. The helper is not a universal document parser and does not decide which tasks are complete.

## Install

The repository root is the skill directory. Choose your agent.

**Codex — macOS / Linux:**

```bash
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/melekhinartem19-hue/project-cleanup.git "$HOME/.agents/skills/project-cleanup"
```

**Claude Code — macOS / Linux:**

```bash
mkdir -p "$HOME/.claude/skills"
git clone https://github.com/melekhinartem19-hue/project-cleanup.git "$HOME/.claude/skills/project-cleanup"
```

**Codex — Windows / PowerShell:**

```powershell
New-Item -ItemType Directory -Force -Path "$HOME/.agents/skills" | Out-Null
git clone https://github.com/melekhinartem19-hue/project-cleanup.git "$HOME/.agents/skills/project-cleanup"
```

**Claude Code — Windows / PowerShell:**

```powershell
New-Item -ItemType Directory -Force -Path "$HOME/.claude/skills" | Out-Null
git clone https://github.com/melekhinartem19-hue/project-cleanup.git "$HOME/.claude/skills/project-cleanup"
```

Start a new agent session after installation. Avoid duplicate same-named copies in directories scanned by one agent. These are local installations; cloud availability depends on the agent. See the official [Codex](https://learn.chatgpt.com/docs/build-skills) and [Claude Code](https://code.claude.com/docs/en/skills) documentation.

For project-only installation, copy `SKILL.md`, `agents/`, `references/`, and `scripts/` into the target project's `.agents/skills/project-cleanup/` or `.claude/skills/project-cleanup/`. Do not overwrite an existing installation without reviewing it. For other agents, use their supported skill directory or explicitly ask them to read this skill's absolute `SKILL.md` path. Individual third-party agents have not been tested.

## Use

Codex chat:

```text
$project-cleanup review this project for completed plans and obsolete
documents or media. Show the report and wait for approval before cleanup.
```

Claude Code chat:

```text
/project-cleanup review this project for completed plans and obsolete
documents or media. Show the report and wait for approval before cleanup.
```

You can limit the scope or request a report only. Approve the entire visible list or an explicit subset. No magic confirmation phrase is required.

## Quarantine and restore

Reports and state live under `<project>/.artifact-cleanup/`. Quarantined data lives in `quarantine/<id>/*.blob`, with original paths and hashes in `journal.json`.

```bash
python3 "$HOME/.agents/skills/project-cleanup/scripts/project_cleanup.py" restore \
  --journal '/path/to/project/.artifact-cleanup/quarantine/<id>/journal.json'
```

On Windows use `python` and a real Windows path; for Claude Code use `.claude` instead of `.agents`. Backups are retained after restore. A moved project root requires separately reviewed recovery.

Quarantine **does not free disk space**. Git and broad searches may still see it; exclude `.artifact-cleanup/**` from searches as needed. The skill does not silently edit `.gitignore` or context settings, and has no permanent-delete command or expiry policy.

## Helper CLI

| Command | Purpose |
| --- | --- |
| `inventory --root ... --out ...` | Read-only artifact metadata inventory |
| `prepare --root ... --selection ... --out ...` | Validate reviewed candidates and pin a plan |
| `apply --plan ... --approve <sha256>` | Quarantine the approved list after rechecking it |
| `restore --journal ...` | Recover files without overwriting different originals |

The `--approve` flag verifies the exact plan file, not human consent. The invoking agent must obtain consent in conversation. Changed files/plans, protected paths, dirty tracked files, traversal, and nested repository candidates are refused. Detailed JSON examples: [operations.md](references/operations.md).

## Update and test

```bash
git -C "$HOME/.agents/skills/project-cleanup" pull --ff-only
# Or, for Claude Code:
git -C "$HOME/.claude/skills/project-cleanup" pull --ff-only
```

From this repository root:

```bash
python -m unittest discover -s tests -v
python scripts/project_cleanup.py --help
```

Tests use isolated temporary projects. CI is configured for Windows/Linux and Python 3.10/3.14. Filesystem-link checks depend on environment support. Semantic classification also received a separate independent agent evaluation; this is not a guarantee for every agent or project.

Do not edit the same files concurrently during cleanup. Existing files do not necessarily enter model context automatically; no exact token savings are promised. Local documents are not uploaded to external services without authorization. Unreadable files and uncertain dependencies are retained. Source/dependency cleanup and installed-skill audits are outside scope.

## License

[MIT](LICENSE).
