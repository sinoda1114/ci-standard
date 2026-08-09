# ci-standard

全リポジトリ共通の標準CI（GitHub Actions Reusable Workflow）。

push / PR で **誰が push しても（人間・Claude・Cursor・Codex いずれでも）常に同じCIが走り、
CIが緑でないと main にマージできない** 状態を作るための中央リポジトリ。
ここのワークフローを改善すると、参照している全リポジトリに即時反映される。

## 構成

| ファイル | 役割 |
|---|---|
| `.github/workflows/node-ci.yml` | Node/TS 標準CI（lint / typecheck / test+カバレッジ / build / audit / e2e / quality） |
| — quality ジョブ | Fallow（未使用コード・重複・複雑度）+ React Doctor（Reactアンチパターン） |
| `.github/workflows/python-ci.yml` | Python 標準CI（ruff / pytest / pip-audit） |
| `templates/node-caller.yml` | 各リポジトリに置く呼び出し側 ci.yml（Node） |
| `templates/python-caller.yml` | 同（Python） |
| `docs/AGENT-PAIRING.md` | クラウド↔ローカルのセッション間連絡の手順（配布しない。必要時に参照） |
| `templates/claude-md-pairing-snippet.md` | 上の存在を知らせる数行。使うプロジェクトの CLAUDE.md に貼る |
| `scripts/setup-ci.sh` | 既存リポジトリへの後付け（ci.yml配置＋ブランチ保護） |

## 導入は自動（sweeper = リポジトリ設定の収束エンジン）

**人間・エージェントの記憶に依存しない。** `.github/workflows/sweeper.yml` が毎日06:00 JSTに
全リポジトリを見回り、[`repo-policy.yml`](repo-policy.yml) が宣言する「あるべき状態」へ収束させる。
新規リポジトリはどう作っても翌朝までに標準が強制される。何度実行しても同じ結果になる（冪等）。

収束させる対象:

| 分類 | 内容 | 適用範囲 |
|---|---|---|
| 運用設定 | `type:*` ラベル（7種・色/説明の是正含む） | 全リポジトリ |
| 運用設定 | Secret scanning / push protection の有効化 | 全リポジトリ（public は無料） |
| 運用設定 | Dependabot 設定の配布（npm / github-actions / weekly） | 全リポジトリ |
| CI/CD | 標準CI呼び出し（ci.yml）の配置 | Node / Python |
| CI/CD | ブランチ保護（CI必須・会話解決必須・admin含む） | 標準CI導入済みのみ |
| CI/CD | コード健全性ゲート（Fallow: 未使用コード/重複/複雑度） | Node（既定 report-only） |
| CI/CD | React アンチパターン検出（React Doctor） | React 系（既定 advisory） |

**型に入れないもの**（理由は repo-policy.yml の `excluded` を参照）: GitHub Project 板の作成
（Status カラム定義が Web UI 必須で冪等化できない）、GitHub 既定ラベルの削除（破壊的）、
セッション間連絡の配布（使うプロジェクトが限られ、かつリポジトリの状態ではない）。

## 全リポジトリに配らないもの: セッション間連絡

クラウドセッションが egress ポリシーで詰まったとき、ローカルセッションへ実測や
ブラウザ操作を依頼できる（逆も可）。手順は [docs/AGENT-PAIRING.md](docs/AGENT-PAIRING.md)。

**配布はしない。** 必要になった時点で raw から取得する（クラウドからは `api.github.com` が
403 でも `raw.githubusercontent.com` は 200）。使うプロジェクトだけ、
[存在を知らせる数行](templates/claude-md-pairing-snippet.md)を `CLAUDE.md` に貼る。

> 設計原則: **冪等に自動化できるものだけを型にする。** 自動化できないものを標準に入れると、
> 毎日「差分あり」と言い続ける壊れた仕組みになる。

初回セットアップ（1回だけ）:

1. fine-grained PAT を作成: Settings → Developer settings → Fine-grained tokens →
   Repository access: **All repositories** / Permissions: **Contents: RW**,
   **Administration: RW**, **Workflows: RW**, **Issues: RW**（ラベル操作に必要）
2. このリポジトリの Settings → Secrets and variables → Actions に `ADMIN_TOKEN` として登録
3. Actions タブ → sweeper → Run workflow で初回実行（以後は毎日自動）

手動での個別導入も可能:

```bash
scripts/setup-ci.sh /path/to/repo   # 1リポジトリ後付け
scripts/sweep.sh                     # ローカルから全リポジトリ見回り（GH_TOKEN必要）
```

## 設計方針

1. **自動判定・グレースフルデグレード**: リポジトリに存在する構成（lint script、playwright.config、tests/ 等）だけ実行。無いステップは黙って skip。PoC リポジトリに入れてもCIは壊れない
2. **必須チェック名は固定**: `ci / build`（Node は加えて `ci / e2e`）。e2e はジョブごと skip されても必須チェックを満たす（GitHub の仕様）
3. **カバレッジ閾値は各リポジトリ側**: vitest.config.ts の `coverage.thresholds` に置く（ラチェット方式: 実測の少し下に設定し退行だけ止める。向上したら引き上げる）
4. **CI用の非シークレット環境変数は `.github/ci.env`**: KEY=VALUE 形式でリポジトリにコミットする（例: Better Auth のCI専用ダミー値）。シークレットは GitHub Secrets + `secrets: inherit`
5. **参照は `@main`**: ソロ運用のため即時反映を優先。壊れる変更を入れる時はブランチで検証してからマージする
6. **ブランチ保護は strict: false**: main 追従の強制はしない（ソロ運用では PR ごとの update-branch 往復が過大なため）
7. **健全性ゲートは report-only で始める**: 既存コードベースに後付けしても赤くならないよう、
   Fallow は `fail-on-issues: false`、React Doctor は `blocking: none` が既定。
   強制したいリポジトリだけ `.github/fallow-strict` を置くと**両方が同時に強制モード**になる。
   **新規PJは最初から置く**（負債が溜まる前なら通せるため）

## AIレビューボット（Cursor Bugbot / Amazon Q / Devin / Socket）

Actions とは別系統（GitHub App）。導入は各サービスの管理画面でリポジトリを追加する。
指摘の裁定ポリシーは各リポジトリの CLAUDE.md を参照
（鵜呑みにせず一次情報で裁定 / 見送り理由をスレッドに返信して resolve / ボットのチェックは必須化しない）。

## 制約

- **private リポジトリのブランチ保護は GitHub Free では設定不可**（403）。CI 自体は動くため、
  マージ運用（緑を確認してからマージ）でカバーする
- Reusable Workflow の参照元にできるよう、このリポジトリは **public** にしている。
  シークレットや固有情報は絶対に置かない

## 既知のドリフト

- 2026-08-08 以前に sweeper が配布した ci.yml には `secrets: inherit` が付いている
  （36リポジトリ）。標準は最小権限化により inherit 無しへ変更済み。呼び出し先の
  標準CIはシークレットを一切参照しないため実害はなく、保護ブランチへは直接push
  できないため一括更新はしない。**各リポジトリを触る機会に PR で除去して収束させる**。
