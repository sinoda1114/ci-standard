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
| 運用設定 | CodeQL default setup（純正 SAST）の有効化 | 全リポジトリ（public は無料 / private は不可） |
| 運用設定 | Dependabot 設定の配布（weekly。node: npm + github-actions / python: pip + github-actions / それ以外: github-actions のみ。sweeper 配布分は言語変更に追従） | 全リポジトリ |
| 運用設定 | PR Bot コメント仕分け（pr-triage 呼び出し + `TYPESAFE_API_KEY`）の配布 | 全リポジトリ（Bot のいる PR でのみ動く） |
| 運用設定 | 骨格ファイル（`AGENTS.md` / `CLAUDE.md` / Issue テンプレ）が無ければ配置。置いたら以後触らない。人が消したら置き直さない | 全リポジトリ（手動保護・ruleset で置けない所は見送り） |
| CI/CD | 標準CI呼び出し（ci.yml）の配置 | Node / Python |
| CI/CD | ブランチ保護（CI必須・会話解決必須・admin含む） | 標準CI導入済みのみ |
| CI/CD | コード健全性ゲート（Fallow: 未使用コード/重複/複雑度） | Node（既定 report-only） |
| CI/CD | React アンチパターン検出（React Doctor） | React 系（既定 advisory） |

sweeper の PR（保護リポジトリへの dependabot / pr-triage）を**マージせずに閉じると「このリポジトリには要らない」と記録し、
以後そのファイルは PR でも直接でも置かない**（保護の有無に関係なく効く）。誤って閉じた場合は、そのファイルの閉じた sweeper PR
（`sweeper/<名前>-*`）**すべて**にラベル `sweeper-superseded` を付ければ翌朝から再提案される。ラベルが無ければ先に作る:
`gh label create sweeper-superseded -R sinoda1114/<repo>`（詳細は `repo-policy.yml`）。

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
   **Administration: RW**, **Workflows: RW**, **Issues: RW**（ラベル操作に必要）,
   **Pull requests: RW**（保護ブランチへの配布を PR で届ける）, **Secrets: RW**（`TYPESAFE_API_KEY` の配布）,
   **Code scanning alerts: RW**（CodeQL default setup の有効化）
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
4. **CI用の非シークレット環境変数は `.github/ci.env`**: KEY=VALUE 形式でリポジトリにコミットする（例: Better Auth のCI専用ダミー値）。シークレットが要るリポだけ `secrets:` で明示マップ（optional。未設定なら空）
5. **参照は `@main`**: ソロ運用のため即時反映を優先。**その裏返しとして、このリポジトリの
   main への変更は全リポジトリのCIに即時波及する**（壊れる変更も同様）。したがって
   **ワークフローの変更は main 直 push ではなく必ずPR経由**にし、マージ前に実リポジトリで
   1本再ランして緑を確認する。
   - 実例: quality ジョブに `pull-requests: write` を要求したところ、Reusable Workflow は
     呼び出し側（`contents: read` のみ）より広い権限を要求できないため、全リポジトリの
     CI が `startup_failure`（ジョブが1つも起動しない）になった。
   - **呼び出し先で `permissions:` を増やさない。** 必要な場合は呼び出し側テンプレートと
     配布済み全リポジトリの ci.yml を先に更新する必要がある（実質不可能なので設計で避ける）
6. **ブランチ保護は strict: false**: main 追従の強制はしない（ソロ運用では PR ごとの update-branch 往復が過大なため）
7. **健全性ゲートは report-only で始める**: 既存コードベースに後付けしても赤くならないよう、
   Fallow は `fail-on-issues: false`、React Doctor は `blocking: none` が既定。
   強制したいリポジトリだけ `.github/fallow-strict` を置くと**両方が同時に強制モード**になる。
   **新規PJは最初から置く**（負債が溜まる前なら通せるため）

## AIレビューボット（Cursor Bugbot / Amazon Q / Devin / Socket）

Actions とは別系統（GitHub App）。導入は各サービスの管理画面でリポジトリを追加する。
指摘の裁定ポリシーは各リポジトリの CLAUDE.md を参照
（鵜呑みにせず一次情報で裁定 / 見送り理由をスレッドに返信して resolve / ボットのチェックは必須化しない）。

## PR Bot コメント仕分け（pr-triage）

5 体の AI レビューボットが同じ問題を別々に書く PR で、`.github/workflows/pr-triage.yml`（実体は本リポの
Reusable Workflow）が Bot のレビュー投稿をきっかけに起動し、3 分待ってスレッドを取得、JEV（TypeSafe AI の
判断専用モデル）で「同じ問題」を束ねて種別順の表を PR の固定コメントに出す。**正誤は判定しない**（人か Claude が
グループ単位で判断）。JEV キーが無いリポジトリではファイル+行の近さで束ねる粗い表になる。
スクリプトは `scripts/pr-triage/`（正本は `~/.claude/skills/pr-triage/scripts/`）。測定根拠は PR #95/#102 で
ペアリング一致 98.6% / 96.6%。費用は 1 PR あたり 1 円未満。

## CI 健全性の見回り（health）

`.github/workflows/health.yml` が毎朝 07:30 JST に全リポジトリを読み、
**既定ブランチの最新 CI が赤のまま**のものと **critical / high の未解決アラート**を
1 本の Issue にまとめる（全部緑になれば自動クローズ）。何も変更しない（読むだけ）。

作った理由: CI は push が無いと走らない。休眠リポジトリでは依存だけが古くなり、
たまに sweeper の配布 push で CI が走って赤くなっても、誰も見ないまま残る。
2026-09-22 の棚卸しで **14 リポジトリ中 8 つが赤のまま放置**され、うち 4 つは
`npm audit` が critical/high を検出して落ちていた（Next.js の未認証 RCE を含む）。
ゲートは正しく働いていたが、気付く経路が無かった。

動作確認は `ONLY="repo-a repo-b" bash scripts/health-watch.sh` でリポジトリを絞れる。

## 制約

- **private リポジトリのブランチ保護は GitHub Free では設定不可**（403）。CI 自体は動くため、
  マージ運用（緑を確認してからマージ）でカバーする
- Reusable Workflow の参照元にできるよう、このリポジトリは **public** にしている。
  シークレットや固有情報は絶対に置かない

## 既知のドリフト

- 2026-08-08 以前に sweeper が配布した ci.yml には `secrets: inherit` が付いている
  （36リポジトリ）。標準は最小権限化により inherit 無しへ変更済み。
  注意: `secrets: inherit` は呼び出し側の**全** secrets を呼び出し先へ渡す（`workflow_call`
  で宣言した名前に限られない）。呼び出し先（このリポジトリの標準CI）は宣言した名前しか
  参照しないコードになっているが、保証はコード側にあり GitHub 側にはない。
  **各リポジトリを触る機会に PR で除去して収束させる**（`setup-ci.sh` / `rollout-all.sh`
  も inherit を出さないよう修正済み）。

## audit の例外リスト（修正版がまだ無い脆弱性）

`node-ci.yml` の audit は high 以上で CI を落とす。修正版が公開されていない脆弱性で CI が止まり続ける場合だけ、
対象リポジトリに `.github/audit-allowlist` を置いて、理由と期限つきで除外できる（npm / pnpm。yarn は従来どおり best-effort）。

```
# GHSA-ID              期限        理由
GHSA-vfj7-8cjw-p6xm    2026-12-31  braces。修正版なし（3.0.3 も影響範囲）。eslint-config-next 経由の開発用依存
```

- 期限の日を過ぎた行は無効になり CI が落ちる。修正版を確認して行を消すか、期限を延ばす（放置を防ぐため）。
- **期限は今日から 120 日以内**に限る。それより先の日付を書いた行は CI を落とす（実質の無期限化を防ぐ）。延ばすときも 120 日以内。
- **critical は例外リストに書いても除外しない**。修正版が無くても、使い方を変えるなどして必ず直す。
- 期限が 14 日以内に迫った行・期限切れの行は、毎朝の health の Issue に載る（private リポジトリは名前と件数だけ）。
- **pnpm の独自の除外設定（`package.json` の `pnpm.auditConfig`、`pnpm-workspace.yaml` の `auditConfig` にある `ignoreGhsas` / `ignoreCves`）は使えない**。理由も期限も残らないため、例外リストの有無にかかわらず見つけたら CI を落とす。除外したいものは例外リストに移す。キー名の大文字小文字・ハイフン・下線の違い（`audit-config` 等）も同じものとして扱う。`pnpm-workspace.yaml` は、重複キーやマージキー（`<<`）でどの値が勝つかを計算せず、`auditConfig` の下に書かれている除外設定（アンカー経由で持ち込んだものも含む）をすべて見て落とす（パーサーによって勝つ値が違うため）。スカラーでないキーも落とす。`${…}` の展開を含むキーと、読めない YAML も落とす。
- 書式の違う行・同じ GHSA の重複も CI を落とす。
- audit 自体の失敗（`{"error": …}`、想定外の形の JSON）は「脆弱性なし」と扱わず CI を落とす。
- GHSA の ID を取り出せない high 以上の指摘は、例外リストで除外できないので常に CI を落とす。
- 期限の判定は UTC の日付（`TZ` の設定に左右されない）。
- `#` で始まる行はコメント。理由の中に `#` を書いてもよい（例: `issue #123 参照`）。
- 使われなくなった行は CI のログに通知が出る。
- **守備範囲**: この仕組みが防ぐのは、理由も期限もない手軽な除外（AI エージェントや開発者が `ignoreGhsas` を足す、期限を何年も先にする、critical を外す等）と、その記録漏れ。呼び出し側のリポジトリを書ける人は自分の `ci.yml` から audit を外すこともできるので、意図的な回避（`.github/ci.env` の環境変数・`.pnpmfile.cjs`・前のステップでの細工など）は対象外とし、レビューで止める。`.npmrc` から pnpm が `auditConfig` を読むかは確かめておらず、検査の対象外。
- ファイルが無いリポジトリは従来の `npm audit --audit-level=high` のまま。判定は `scripts/audit-gate.py`（テストは `scripts/test_audit_gate.py`）。
- `.github/audit-omit-dev` があるリポジトリだけ、監査対象を本番依存に限る（npm は `npm audit --omit=dev`、pnpm は `pnpm audit --prod`）。high 以上で落とすことと、例外リストの判定は変えない。ファイルが無いリポジトリは dev 依存も監査する。
- 判定スクリプトは、呼び出されたワークフローと版がずれないよう `node-ci.yml` に埋め込んである。`scripts/audit-gate.py` を直したら埋め込みも同じ内容にする（一致しないと self-test が落ちる）。
