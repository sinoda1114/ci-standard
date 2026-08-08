# CD標準（デプロイ・監視）

CIの後段。「main にマージされたら、人間の記憶に頼らず本番に反映され、壊れたら気づける」を
全リポジトリ共通の仕組みにする。

## 原則

1. **mainマージ＝自動デプロイに一本化**: ローカルでの `npm run deploy` 手動実行は原則廃止
   （デプロイ忘れ・手元状態のデプロイという2大事故を構造的に排除）
2. **デプロイはCI成功の後段**: deploy.yml 内で 標準CI → deploy を直列実行。CIが赤なら本番に届かない
3. **デプロイ後の疎通確認まで自動**: `.github/ci.env` に `DEPLOY_HEALTHCHECK_URL=...` を
   設定すると、デプロイ直後にHTTP確認して失敗ならワークフローを赤にする
4. **監視は中央一元**: 本番URLは ci-standard の `monitor-urls.txt` に登録。30分ごとに疎通確認し、
   落ちていれば ci-standard に Issue が自動起票される

## 対象リポジトリへの導入（Cloudflare Pages/Workers）

1. `templates/deploy-cloudflare-caller.yml` を対象リポジトリの `.github/workflows/deploy.yml` に置く
2. 対象リポジトリの Settings → Secrets and variables → Actions に登録:
   - `CLOUDFLARE_API_TOKEN`（My Profile → API Tokens。Pages:Edit の最小権限で作成）
   - `CLOUDFLARE_ACCOUNT_ID`（ダッシュボード右下）
3. 既存の `ci.yml` のトリガーを `pull_request` のみに変更する
   （main push の検証は deploy.yml 内のCIが担うため。二重実行の無駄を防ぐ）
4. 本番URLを ci-standard の `monitor-urls.txt` に追加する
5. `package.json` の `deploy` スクリプトが非対話で完結することを確認
   （wrangler は CLOUDFLARE_API_TOKEN があれば login 不要）

Vercel 系は同じ構造の deploy-vercel.yml を必要になった時点で追加する
（shinoda-dev-lp が第一候補。Vercel は git 連携でも代替可能なので、その選択も含めて判断）。

## ロールバック手順（Cloudflare Pages）

```bash
# デプロイ履歴を確認
npx wrangler pages deployment list --project-name <project>

# 直前の正常版に戻す（コミットを戻すのではなく配信を戻す＝即効）
npx wrangler pages deployment rollback --project-name <project>
```

その後、原因コミットを `git revert` して main を正す（配信のロールバックはあくまで応急処置。
main と本番の内容が乖離したまま放置しない）。

## 監視の運用

- 障害Issueは自動起票・**クローズは人間**（回復確認してから閉じる。誤クローズ防止）
- 監視対象の追加/削除は `monitor-urls.txt` の編集のみ（PRでもmain直コミットでも可）
- 30分間隔・リトライ1回。より短い間隔や外形監視の高度化が必要になったら外部サービスを検討
