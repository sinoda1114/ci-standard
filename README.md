# ci-standard

全リポジトリ共通の標準CI（GitHub Actions Reusable Workflow）。

push / PR で **誰が push しても（人間・Claude・Cursor・Codex いずれでも）常に同じCIが走り、
CIが緑でないと main にマージできない** 状態を作るための中央リポジトリ。
ここのワークフローを改善すると、参照している全リポジトリに即時反映される。

## 構成

| ファイル | 役割 |
|---|---|
| `.github/workflows/node-ci.yml` | Node/TS 標準CI（lint / typecheck / test+カバレッジ / build / audit / e2e） |
| `.github/workflows/python-ci.yml` | Python 標準CI（ruff / pytest / pip-audit） |
| `templates/node-caller.yml` | 各リポジトリに置く呼び出し側 ci.yml（Node） |
| `templates/python-caller.yml` | 同（Python） |
| `scripts/setup-ci.sh` | 既存リポジトリへの後付け（ci.yml配置＋ブランチ保護） |

## 各リポジトリへの導入

```bash
scripts/setup-ci.sh /path/to/repo
# 配置された .github/workflows/ci.yml を確認してコミット・push
```

## 設計方針

1. **自動判定・グレースフルデグレード**: リポジトリに存在する構成（lint script、playwright.config、tests/ 等）だけ実行。無いステップは黙って skip。PoC リポジトリに入れてもCIは壊れない
2. **必須チェック名は固定**: `ci / build`（Node は加えて `ci / e2e`）。e2e はジョブごと skip されても必須チェックを満たす（GitHub の仕様）
3. **カバレッジ閾値は各リポジトリ側**: vitest.config.ts の `coverage.thresholds` に置く（ラチェット方式: 実測の少し下に設定し退行だけ止める。向上したら引き上げる）
4. **CI用の非シークレット環境変数は `.github/ci.env`**: KEY=VALUE 形式でリポジトリにコミットする（例: Better Auth のCI専用ダミー値）。シークレットは GitHub Secrets + `secrets: inherit`
5. **参照は `@main`**: ソロ運用のため即時反映を優先。壊れる変更を入れる時はブランチで検証してからマージする
6. **ブランチ保護は strict: false**: main 追従の強制はしない（ソロ運用では PR ごとの update-branch 往復が過大なため）

## AIレビューボット（Cursor Bugbot / Amazon Q / Devin / Socket）

Actions とは別系統（GitHub App）。導入は各サービスの管理画面でリポジトリを追加する。
指摘の裁定ポリシーは各リポジトリの CLAUDE.md を参照
（鵜呑みにせず一次情報で裁定 / 見送り理由をスレッドに返信して resolve / ボットのチェックは必須化しない）。

## 制約

- **private リポジトリのブランチ保護は GitHub Free では設定不可**（403）。CI 自体は動くため、
  マージ運用（緑を確認してからマージ）でカバーする
- Reusable Workflow の参照元にできるよう、このリポジトリは **public** にしている。
  シークレットや固有情報は絶対に置かない
