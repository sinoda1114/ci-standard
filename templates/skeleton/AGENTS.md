<!-- sweeper が配布した骨格（sinoda1114/ci-standard）。固有値を埋めたらこの行を消す。消すと sweeper は以後このファイルを触らない -->
# AGENTS.md — AI エージェント向けプロジェクト指示

Claude Code / Codex / Cursor 共通の入口です。運用ルールの正本は作者の `~/.claude/CLAUDE.md`（要約は
[claude-project-starter の notes/](https://github.com/sinoda1114/claude-project-starter/tree/main/notes)）で、
ここには他ツールでも守れる最小限の要約と、このリポ固有の値だけを置きます。

## 運用ルール（要約）

- main に直接 commit / push しない。`feat|fix|chore/<topic>` ブランチ → PR → squash マージ。
- ブランチは `git fetch origin` の後に `origin/main` 起点で切る。並行作業は 1 エージェント = 1 worktree。
- push 前にレビューを通す（Claude Code は `/ai-review`）。High があれば push しない。
- 標準 CI（sinoda1114/ci-standard）が緑になってからマージする。デプロイは git 駆動（main マージ = 本番）で、手動デプロイしない。
- タスクと PR の状態は GitHub（Issue / Project / `gh pr view`）を正とし、記憶で語らない。
- テスト先行（TDD）。検証は AI 自身が実行して PASS / FAIL を示す。
- lockfile をコミットし、CI は `npm ci`。秘密情報をログ・出力・コミットに出さない。

## このプロジェクト固有の値（TODO: 埋める）

| 項目 | 値 |
|---|---|
| 目的 | |
| 技術スタック | |
| デプロイ先 / 本番 URL | |
| タスク管理（Project 板） | |
