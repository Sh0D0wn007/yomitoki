<p align="center">
  <img src="assets/logo.svg" alt="yomitoki" width="560">
</p>

<p align="center">
  <b>🇯🇵 日本語</b> | <a href="README.zh-CN.md">🇨🇳 简体中文</a> | <a href="README.en.md">🇺🇸 English</a>
</p>

# yomitoki

コードの理解にかかる時間を減らすための、AIエージェント向けスキルです。[Agent Skills](https://agentskills.io/) 形式の `SKILL.md` で書かれており、Claude Code・Codex・Gemini CLI・Cursor・GitHub Copilot などの対応エージェントで使えます。命名・制御フロー・変数・責務・コメント・テストの見直しに加え、Python・C・C++・Java・JavaScript・TypeScriptの機械検査と修正前後の比較を行います。

**紹介動画：実コードで見る改善前後**

https://github.com/user-attachments/assets/1c9d27cf-d481-47d6-bb6b-0ae146cda7e0

ペリカンのアニメーションを題材に、描画順と詳細の分離、命名の変更を実コードで比較します。関数数・コード行数の変化と、検証できた範囲も紹介しています。

- `scripts/check.py`：関数の行数・引数数・ネスト・複雑さ・入れ子の三項式を検査（Python・C・C++・Java・JavaScript・TypeScript）
- `scripts/run_checks.py`：構文指標と、PythonのRuff lint・整形を一括検査
- `scripts/compare_reports.py`：検査条件・対象範囲・指摘数・指標の変化を比較
- `scripts/diff_code.py`：変更前後のPythonソースから構造の変化と確認位置を抽出

作業は「文脈と契約の確認 → 変更前の保存・機械検査 → 小さな修正 → 数値とソース差分の点検 → テスト」の順に進めます。コードはレビューでも修正でも [判断基準](references/principles.md) の14項目すべてに照らして確認し、指摘と変更には該当する項目の番号を付けて報告します。差分検査の使い方は [変更内容の点検](references/change-review.md) を参照してください。

## 対応言語

| 対象 | 付属スクリプト | 併用する導入済みツール |
| --- | --- | --- |
| Python | 構文指標・Ruff lint・整形 | プロジェクト既存のlint・型チェック |
| C・C++ | 構文指標 | clang-tidy・Cppcheck |
| Java | 構文指標 | PMD・Checkstyle |
| JavaScript・TypeScript | 構文指標 | ESLint・Biome・Oxlint・tsc |
| HTML・CSS | なし | HTML-validate・Stylelint |

構文指標（関数の行数・引数数・ネスト・複雑さ・入れ子の三項演算子）は、Pythonでは標準ライブラリの`ast`、その他の言語では [tree-sitter](https://tree-sitter.github.io/) の構文木から同じ定義で測ります。Ruffに当たるlint・整形は、Python以外では対象プロジェクトの導入済みツールで検査します。付属スクリプトはこれらのツールを導入・実行しません。選び方・実行例・変更前後の比較は [言語別の機械検査](references/language-checks.md) を参照してください。

## セットアップ

### npxでインストール

npm（npx）と、Node.js **24系の24.22.1以降（推奨）** または **22系の22.23.4以降** を使用してください。各メジャーバージョンの最新パッチ版を選びます。

これらは [2026年10月21日のセキュリティリリース](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases) の修正を含むバージョンです（2026年10月25日確認）。利用するプロジェクトのルートで実行します。

```bash
npx skills add Sh0D0wn007/yomitoki --skill yomitoki
```

画面の案内に従って利用するエージェントを選びます。標準ではプロジェクト単位でインストールされます。全プロジェクトで使う場合は `--global` を付けます。例えば、CodexとClaude Codeの両方で使う場合：

```bash
npx skills add Sh0D0wn007/yomitoki --skill yomitoki --global --agent codex claude-code
```

この方法では配置とリンクの作成をCLIが行うため、手動配置は不要です。Pythonの検査用依存パッケージは別途、後述の手順で導入します。詳しくは [skills CLIの公式ドキュメント](https://github.com/vercel-labs/skills/blob/main/docs/commands.md) を参照してください。

### 手動で配置する場合

このリポジトリを、使うエージェントのスキル用ディレクトリへ `yomitoki` という名前で配置します。`~/.agents/skills/` は複数のエージェントが共通で読み込むため、ここに置けば1か所で共有できます。

| エージェント | ユーザー全体 | プロジェクト単位 |
| --- | --- | --- |
| Codex・Gemini CLI・Cursor・GitHub Copilot | `~/.agents/skills/yomitoki` | `.agents/skills/yomitoki` |
| Claude Code | `~/.claude/skills/yomitoki` | `.claude/skills/yomitoki` |

Claude Code と併用する場合は、`~/.agents/skills` に置いた実体へのシンボリックリンクを作ると、更新が両方に反映されます。

```bash
mkdir -p ~/.claude/skills
ln -s ~/.agents/skills/yomitoki ~/.claude/skills/yomitoki
```

上記以外の [Agent Skills 対応エージェント](https://agentskills.io/) では、各ドキュメントに記載されたスキルの配置先に置きます。既存のスキルがある場合は内容を確認し、バックアップしてから移行してください。スキルの指示は [SKILL.md](SKILL.md) にあります。`agents/openai.yaml` は Codex の画面表示用の任意設定です。

### 付属の検査を使う場合

Python 3.10以上を使用してください。Python以外の構文解析に使うtree-sitterはPython 3.10以上が必要です。Python 3.9ではPythonファイルだけを検査できます。Pythonを検査する場合は、対象コードの構文に対応したPythonを使用してください。仮想環境は移動すると動かなくなるため、配置した後のディレクトリで作成します。以下はプロジェクト単位でインストールした場合の例です。ユーザー全体にインストールした場合は `~/.agents/skills/yomitoki`、手動配置した場合はその配置先へ移動してください。

macOSに標準で入っている`python3`（Xcodeのコマンドラインツール付属）は3.9のことがあります。`python3 --version`で確認し、3.9なら3.10以上のPython（例：Homebrewの`python3.12`）で仮想環境を作ります。3.9で作ると、tree-sitterが導入されず、Python以外のファイルが検査エラーになります。

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

Windowsでは、以降のコマンドの`python3`を`python`に、`.venv/bin/`を`.venv\Scripts\`（Git Bashでは`.venv/Scripts/`）に読み替えます。

## 実行

リポジトリのルートで、セットアップした`.venv`のPythonを使って実行します。

```bash
.venv/bin/python scripts/run_checks.py --json /path/to/project > before.json
# 対象コードを修正した後、同じ条件で再検査
.venv/bin/python scripts/run_checks.py --json /path/to/project > after.json
.venv/bin/python scripts/compare_reports.py before.json after.json
```

PowerShellで`>`を使って保存するときは、先に`[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`を実行します。Windows PowerShell 5.1では、実行しないと日本語を含む出力が壊れ、比較できません。

検査は指摘ありで終了コード`1`、実行失敗で`2`を返します。比較は条件不一致も`2`です。詳しい設定、比較方法、終了コードは [一括検査と前後比較](references/workflow-checks.md) を参照してください。

Pythonファイルの構文指標だけを、追加パッケージなしで検査する場合：

```bash
python3 scripts/check.py /path/to/project
```

閾値は見直し候補を抽出するための初期設定です。指摘なしでも、命名の適切さや挙動の同一性を保証しません。[判断基準](references/principles.md) と [指標の定義](references/mechanical-checks.md) を併せて確認してください。

## 検証

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

Python 3.9では`match`構文のテストと、tree-sitterを使う他言語のテストをスキップします。Python 3.10以上で`requirements.txt`を導入すると全ケースを実行できます。
