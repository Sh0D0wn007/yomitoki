<p align="center">
  <img src="assets/logo.svg" alt="yomitoki" width="560">
</p>

<p align="center">
  <a href="README.md">🇯🇵 日本語</a> | <a href="README.zh-CN.md">🇨🇳 简体中文</a> | <b>🇺🇸 English</b>
</p>

# yomitoki

A skill for AI agents that reduces the time it takes to understand code. It is written as a `SKILL.md` in the [Agent Skills](https://agentskills.io/) format and works with compatible agents such as Claude Code, Codex, Gemini CLI, Cursor, and GitHub Copilot. In addition to reviewing naming, control flow, variables, responsibilities, comments, and tests, it runs mechanical checks on Python, C, C++, Java, JavaScript, and TypeScript code and compares the results before and after changes.

**Demo video: Before and after improvements in real code**

https://github.com/user-attachments/assets/09d12c3f-adbe-4821-86a6-cb25c6b5a8da

Using a pelican animation as an example, the video compares real code before and after separating drawing order from implementation details and improving names. It also shows changes in function count and lines of code, along with the scope of verification.

- `scripts/check.py`: Checks function length, argument count, nesting, complexity, and nested ternary expressions (Python, C, C++, Java, JavaScript, TypeScript)
- `scripts/run_checks.py`: Runs syntax metrics plus Ruff lint and format checks for Python in one pass
- `scripts/compare_reports.py`: Compares check conditions, target scope, finding counts, and metric changes
- `scripts/diff_code.py`: Extracts structural changes and locations to review from Python sources before and after a change
- `scripts/review_session.py`: Combines current-source snapshots and before checks in `begin`, then after checks, comparison, and saved diffs in `finish` (never modifies or executes target code)

Use `begin --state-root /path/outside/reviews` to create a unique session inside an outside-target parent directory, including an existing work directory. Pass the returned `stateDir` to `finish --state-dir`. The existing `begin --state-dir` option still accepts an exact path that does not yet exist. See the [execution examples](references/review-session.md) (Japanese) for UTF-8 settings from the first PowerShell call.

Work proceeds in this order: "confirm context and contracts → save the original and run mechanical checks → make small fixes → inspect the metrics and source diff → test". For both reviews and fixes, the code is checked against all 14 items of the [Judgment Criteria](references/principles.md) (Japanese), and each finding and change is reported with the number of the item it concerns. For how to use the diff checks, see [Reviewing Changes](references/change-review.md) (Japanese).

See [Combined checks and comparison](references/review-session.md) (Japanese) for the combined snapshot and checking workflow. It preserves the criteria and verification scope while avoiding repeated scans for a different display format or unchanged code. The helper does not run project tests, lint, type checks, or builds.

For small targets, `begin --context` returns the full sources and criteria together, and `finish --include-diffs` returns all diffs together. Record evidence for each of the 14 criteria; `--review-notes` detects missing or unverified items. Complete records do not guarantee correct judgments.

## Supported languages

| Target | Bundled scripts | Installed tools to use alongside |
| --- | --- | --- |
| Python | Syntax metrics, Ruff lint, formatting | The project's existing lint and type checks |
| C, C++ | Syntax metrics | clang-tidy, Cppcheck |
| Java | Syntax metrics | PMD, Checkstyle |
| JavaScript, TypeScript | Syntax metrics | ESLint, Biome, Oxlint, tsc |
| HTML, CSS | None | HTML-validate, Stylelint |

Syntax metrics (function length, argument count, nesting, complexity, nested ternaries) use the same definitions in every language: Python is measured with the standard library's `ast`, and the other languages with [tree-sitter](https://tree-sitter.github.io/) syntax trees. For languages other than Python, lint and formatting (Ruff's role) are checked with tools already installed in the target project; the bundled scripts do not install or run them. See [Language-specific checks](references/language-checks.md) (Japanese) for selection, commands, and before/after comparison.

## Setup

### Install with npx

Use npm (npx) with Node.js **24.22.1 or later in the 24.x line (recommended)**, or **22.23.4 or later in the 22.x line**. Choose the latest patch release in your chosen major version.

These versions include the fixes from the [October 21, 2026 security release](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases) (verified October 25, 2026). Run from your project's root:

```bash
npx skills add Sh0D0wn007/yomitoki --skill yomitoki
```

Follow the prompts to select your agents. Installation is per-project by default. Add `--global` to use the skill across all projects. For example, to install for both Codex and Claude Code:

```bash
npx skills add Sh0D0wn007/yomitoki --skill yomitoki --global --agent codex claude-code
```

The CLI handles placement and links, so manual placement is unnecessary. Install the Python check dependencies separately as described below. See the [official skills CLI documentation](https://github.com/vercel-labs/skills/blob/main/docs/commands.md) for details.

### Manual placement

Place this repository in your agent's skills directory under the name `yomitoki`. `~/.agents/skills/` is read by multiple agents, so placing it there lets you share it from a single location.

| Agent | User-wide | Per-project |
| --- | --- | --- |
| Codex, Gemini CLI, Cursor, GitHub Copilot | `~/.agents/skills/yomitoki` | `.agents/skills/yomitoki` |
| Claude Code | `~/.claude/skills/yomitoki` | `.claude/skills/yomitoki` |
| Antigravity | `~/.gemini/skills/yomitoki` | `.agents/skills/yomitoki` |

If you also use Claude Code, create a symbolic link to the copy in `~/.agents/skills` so that updates apply to both.

```bash
mkdir -p ~/.claude/skills
ln -s ~/.agents/skills/yomitoki ~/.claude/skills/yomitoki
```

When using with Antigravity, linking `~/.gemini/skills` to `~/.agents/skills` ensures that any skills installed or updated via `npx skills add -g` are automatically reflected in Antigravity as well. On Windows, creating an NTFS Junction is recommended as it does not require administrator privileges.

macOS and Linux:

```bash
mkdir -p ~/.gemini
ln -s ~/.agents/skills ~/.gemini/skills
```

Windows (PowerShell):

```powershell
New-Item -ItemType Junction -Path "$HOME\.gemini\skills" -Target "$HOME\.agents\skills"
```

For other [Agent Skills-compatible agents](https://agentskills.io/), place it in the skills location described in each agent's documentation. If you already have an existing skill, review its contents and back it up before migrating. The skill's instructions are in [SKILL.md](SKILL.md). `agents/openai.yaml` is an optional setting for how the skill is displayed in Codex.

### Using the bundled checks

Use Python 3.10 or later. tree-sitter, which parses languages other than Python, requires Python 3.10 or later; on Python 3.9 only Python files can be checked. When checking Python, use a Python version that supports the syntax of the target code. Virtual environments break when moved, so create one in the directory where you placed the skill. The example below assumes a per-project installation. For a global installation, use `~/.agents/skills/yomitoki`; for manual placement, use your chosen directory.

The `python3` that ships with macOS (from the Xcode Command Line Tools) may be 3.9. Check with `python3 --version`; if it is 3.9, create the virtual environment with Python 3.10 or later (for example, Homebrew's `python3.12`). With 3.9, tree-sitter is not installed and files other than Python fail to check.

macOS and Linux:

```bash
cd .agents/skills/yomitoki
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
cd .agents\skills\yomitoki
python --version
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On Windows, read `python3` in the following commands as `python`, and `.venv/bin/` as `.venv\Scripts\` (`.venv/Scripts/` in Git Bash).

## Usage

Run from the repository root with the Python in the `.venv` you set up.

```bash
.venv/bin/python scripts/run_checks.py --json /path/to/project > before.json
# After modifying the target code, re-check under the same conditions
.venv/bin/python scripts/run_checks.py --json /path/to/project > after.json
.venv/bin/python scripts/compare_reports.py before.json after.json
```

When saving with `>` in PowerShell, first run `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`. In Windows PowerShell 5.1, output containing Japanese is otherwise corrupted and cannot be compared.

Checks exit with code `1` when there are findings and `2` on execution failure. Comparison also returns `2` when the conditions do not match. For detailed settings, comparison methods, and exit codes, see [Batch Checks and Before/After Comparison](references/workflow-checks.md) (Japanese).

To check only the syntax metrics of Python files without additional packages:

```bash
python3 scripts/check.py /path/to/project
```

The thresholds are initial settings for surfacing candidates for review. No findings does not guarantee appropriate naming or unchanged behavior. See also [Judgment Criteria](references/principles.md) and [Metric Definitions](references/mechanical-checks.md) (Japanese).

## Testing

macOS and Linux:

```bash
YOMITOKI_RUFF="$PWD/.venv/bin/ruff" .venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/ruff check --isolated --no-cache --select E9,F scripts tests
```

Windows (PowerShell):

```powershell
$env:YOMITOKI_RUFF = "$PWD\.venv\Scripts\ruff.exe"
.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.venv\Scripts\ruff.exe check --isolated --no-cache --select E9,F scripts tests
```

On Python 3.9, the `match` statement test and the tree-sitter tests for other languages are skipped. On Python 3.10 or later with `requirements.txt` installed, the test for a missing tree-sitter installation is skipped. Symlink tests may also be skipped depending on the environment. CI is configured to test Python 3.10, 3.11, and 3.12 on Ubuntu and Windows.

`scripts/` itself does not follow the bundled checks' default thresholds and formatting settings, so `run_checks.py scripts tests` reports findings.
