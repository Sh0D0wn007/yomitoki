<p align="center">
  <img src="assets/logo.svg" alt="yomitoki" width="560">
</p>

<p align="center">
  <a href="README.md">🇯🇵 日本語</a> | <b>🇨🇳 简体中文</b> | <a href="README.en.md">🇺🇸 English</a>
</p>

# yomitoki

一个面向 AI 智能体的技能，用于减少理解代码所需的时间。它以 [Agent Skills](https://agentskills.io/) 格式的 `SKILL.md` 编写，可在 Claude Code、Codex、Gemini CLI、Cursor、GitHub Copilot 等兼容的智能体中使用。除了审视命名、控制流、变量、职责、注释和测试之外，还会对 Python、C、C++、Java、JavaScript 和 TypeScript 代码进行机械检查，并比较修改前后的结果。

**介绍视频：通过实际代码对比改进前后**

https://github.com/user-attachments/assets/1c9d27cf-d481-47d6-bb6b-0ae146cda7e0

以鹈鹕动画为例，通过实际代码对比绘制顺序与实现细节的分离，以及命名的修改。同时介绍函数数量、代码行数的变化和已验证的范围。

- `scripts/check.py`：检查函数的行数、参数数量、嵌套、复杂度以及嵌套的三元表达式（Python、C、C++、Java、JavaScript、TypeScript）
- `scripts/run_checks.py`：一次性检查语法指标，以及 Python 的 Ruff lint 和格式
- `scripts/compare_reports.py`：比较检查条件、目标范围、问题数量和指标的变化
- `scripts/diff_code.py`：从修改前后的 Python 源码中提取结构变化和需要确认的位置

工作按照"确认上下文与约定 → 保存修改前的状态并进行机械检查 → 小幅修改 → 核对数值与源码差异 → 测试"的顺序进行。差异检查的用法请参阅 [变更内容检查](references/change-review.md)（日语）。

## 支持的语言

| 对象 | 附带脚本 | 配合使用的已安装工具 |
| --- | --- | --- |
| Python | 语法指标、Ruff lint、格式 | 项目现有的 lint 和类型检查 |
| C、C++ | 语法指标 | clang-tidy、Cppcheck |
| Java | 语法指标 | PMD、Checkstyle |
| JavaScript、TypeScript | 语法指标 | ESLint、Biome、Oxlint、tsc |
| HTML、CSS | 无 | HTML-validate、Stylelint |

语法指标（函数行数、参数数量、嵌套、复杂度、嵌套的三元运算符）在所有语言中使用相同的定义：Python 使用标准库的 `ast`，其他语言使用 [tree-sitter](https://tree-sitter.github.io/) 的语法树进行测量。对于 Python 以外的语言，相当于 Ruff 的 lint 和格式检查请使用目标项目已安装的工具，附带脚本不会安装或运行这些工具。选择方法、命令示例及修改前后对比请参阅 [分语言机械检查](references/language-checks.md)（日语）。

## 安装

### 使用npx安装

请使用npm（npx）和Node.js **24.x系列的24.22.1或更高版本（推荐）**，或 **22.x系列的22.23.4或更高版本**。请选择所用主版本的最新补丁版本。

这些版本包含 [2026年10月21日安全更新](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases) 中的修复（2026年10月25日确认）。在项目根目录中运行：

```bash
npx skills add Sh0D0wn007/yomitoki --skill yomitoki
```

按照提示选择所用的智能体。默认安装到当前项目；如需在所有项目中使用，请加上 `--global`。例如，同时为Codex和Claude Code安装：

```bash
npx skills add Sh0D0wn007/yomitoki --skill yomitoki --global --agent codex claude-code
```

CLI会自动放置文件并创建链接，无需手动放置。Python检查所需的依赖包需按照下文另行安装。详情请参阅 [skills CLI官方文档](https://github.com/vercel-labs/skills/blob/main/docs/commands.md)。

### 手动放置

将本仓库以 `yomitoki` 为名放置到所用智能体的技能目录中。`~/.agents/skills/` 会被多个智能体共同读取，放在这里即可在一处共享。

| 智能体 | 用户全局 | 项目级 |
| --- | --- | --- |
| Codex、Gemini CLI、Cursor、GitHub Copilot | `~/.agents/skills/yomitoki` | `.agents/skills/yomitoki` |
| Claude Code | `~/.claude/skills/yomitoki` | `.claude/skills/yomitoki` |

与 Claude Code 一同使用时，创建指向 `~/.agents/skills` 中实体的符号链接，更新即可同时反映到两者。

```bash
mkdir -p ~/.claude/skills
ln -s ~/.agents/skills/yomitoki ~/.claude/skills/yomitoki
```

对于上述以外的 [支持 Agent Skills 的智能体](https://agentskills.io/)，请放置到各自文档中所述的技能目录。如果已有现有技能，请先确认内容并备份后再迁移。技能的指令位于 [SKILL.md](SKILL.md)。`agents/openai.yaml` 是用于 Codex 界面显示的可选设置。

### 使用附带的检查

请使用 Python 3.10 或更高版本。用于解析 Python 以外语言的 tree-sitter 需要 Python 3.10 或更高版本；在 Python 3.9 中只能检查 Python 文件。检查 Python 时，请使用支持目标代码语法的 Python。虚拟环境移动后将无法使用，因此请在放置后的目录中创建。以下示例适用于项目级安装。全局安装时请进入 `~/.agents/skills/yomitoki`，手动放置时请进入所选目录。

macOS 自带的 `python3`（随 Xcode 命令行工具提供）可能是 3.9。请用 `python3 --version` 确认；如果是 3.9，请使用 Python 3.10 或更高版本（例如 Homebrew 的 `python3.12`）创建虚拟环境。使用 3.9 时不会安装 tree-sitter，Python 以外的文件将出现检查错误。

macOS・Linux：

```bash
cd .agents/skills/yomitoki
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Windows（PowerShell）：

```powershell
cd .agents\skills\yomitoki
python --version
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

在 Windows 中，请将后续命令中的 `python3` 替换为 `python`，将 `.venv/bin/` 替换为 `.venv\Scripts\`（在 Git Bash 中为 `.venv/Scripts/`）。

## 运行

在仓库根目录中，使用已设置的 `.venv` 中的 Python 运行。

```bash
.venv/bin/python scripts/run_checks.py --json /path/to/project > before.json
# 修改目标代码后，以相同条件重新检查
.venv/bin/python scripts/run_checks.py --json /path/to/project > after.json
.venv/bin/python scripts/compare_reports.py before.json after.json
```

在 PowerShell 中用 `>` 保存时，请先运行 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`。在 Windows PowerShell 5.1 中，如果不先运行，包含日语的输出会损坏，无法进行比较。

检查发现问题时返回退出码 `1`，执行失败时返回 `2`。比较时条件不一致也返回 `2`。详细设置、比较方法和退出码请参阅 [批量检查与前后比较](references/workflow-checks.md)（日语）。

如需在不安装额外包的情况下仅检查 Python 文件的语法指标：

```bash
python3 scripts/check.py /path/to/project
```

阈值是用于筛选待审视候选项的初始设置。即使没有发现问题，也不能保证命名恰当或行为不变。请同时参阅 [判断标准](references/principles.md) 和 [指标定义](references/mechanical-checks.md)（日语）。

## 验证

macOS・Linux：

```bash
YOMITOKI_RUFF="$PWD/.venv/bin/ruff" .venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/ruff check --isolated --no-cache --select E9,F scripts tests
```

Windows（PowerShell）：

```powershell
$env:YOMITOKI_RUFF = "$PWD\.venv\Scripts\ruff.exe"
.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.venv\Scripts\ruff.exe check --isolated --no-cache --select E9,F scripts tests
```

在 Python 3.9 中会跳过 `match` 语句的测试以及使用 tree-sitter 的其他语言测试。在 Python 3.10 及以上版本中安装 `requirements.txt` 后可以运行全部用例。
