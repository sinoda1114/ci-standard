# AIレビューボット展開ガイド

pj-pilot PR #33 で実証済みの4ボットを、他リポジトリへ展開するための手順と費用判断（2026-08時点の調査）。
いずれも GitHub App のため **導入は各サービスの管理画面での操作が必要**（Actionsと違い自動配布不可）。
指摘の裁定ポリシーは各リポジトリの CLAUDE.md「AIレビューボット指摘の裁定ポリシー」節を参照。

## 費用と推奨展開範囲（ソロ運用・大半private前提)

| ボット | 費用 | 推奨展開 | 理由 |
|---|---|---|---|
| **Amazon Q Developer** | **無料**（プレビュー、月間行数制限あり） | ✅ **全リポジトリ** | 無料で全展開できる唯一のセキュリティ系。誤検知あり（PR #33で5件中2件）だが裁定ポリシーで運用可 |
| **Devin Review** | **無料**（GitHub PR。privateはDevinアカウント要 = 保有済み） | ✅ **全リポジトリ** | 無料・提案diff付きで質が高かった。ステータスチェックが解決しない癖のみ注意（必須化しない運用で吸収済み） |
| **Cursor Bugbot** | **従量課金 約$1〜1.5/PRレビュー**（2026-06に定額$40/月から移行） | ✅ **全リポジトリ＋月間支出上限** | 課金はPRレビュー実行ごと＝放置リポジトリはコストゼロ。上限（ハードキャップ）で防衛し、リポジトリ選択の手動管理を排除 |
| **Socket Security** | **publicは無料** / privateはTeam $25/席/月 | ✅ **全リポジトリ** | 無料プランではprivateにレポートが出ないだけで課金も故障もない。publicは自動カバー、有料化すればprivateも即有効 |

## 導入手順

### Amazon Q Developer（全リポジトリ）
1. https://github.com/apps/amazon-q-developer → Install/Configure
2. Repository access で **All repositories** を選択
3. 以後、PR作成時に自動レビュー。月間行数制限に達したら翌月回復（AWSアカウント紐付けで拡張可、ただし従量化するので当面不要）

### Devin Review（全リポジトリ）
1. Devin にログイン → Integrations（または https://github.com/apps/devin-ai-integration）→ Configure
2. Repository access で **All repositories** を選択

### Cursor Bugbot（全リポジトリ＋支出上限）
1. https://github.com/apps/cursor → Configure → **All repositories** → Save
2. Cursor ダッシュボードで **spending limit（月間上限）** を必ず設定（唯一のコスト防衛線。例: $20/月）

### Socket Security（全リポジトリ）
1. https://github.com/apps/socket-security → Configure → **All repositories** → Save
2. 無料プランのためレポートは public のみ。private は課金せず単に何も起きない（有料化すれば即有効）

## 運用ルール（再掲）

- ボットのステータスチェックは**ブランチ保護の必須に含めない**（successに解決しないことがある。必須は `ci / build` / `ci / e2e` のみ）
- 指摘は鵜呑みにせず一次情報で裁定。見送りは理由をスレッドに返信して resolve（会話解決必須化がゲートとして効く）
- **4ボットすべて All repositories** = 新規リポジトリは自動でカバーされる（sweeperと同じ思想・冪等・追加作業ゼロ）。コスト制御はリポジトリ選択ではなく Cursor の月間支出上限で行う
