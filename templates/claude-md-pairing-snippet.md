<!--
プロジェクトの CLAUDE.md に貼る「存在を知らせるためだけ」の数行。
本文は配布しない（正本は ci-standard/docs/AGENT-PAIRING.md）。

貼るのは、クラウドとローカルで並行作業するプロジェクトだけでよい。
以下の --- から下をコピーする。
-->

---

## クラウド↔ローカルのセッション間連絡

クラウドセッションで **egress ポリシーに阻まれて作業が止まったら**（Vercel / Turso /
Google Cloud の確認、ブラウザ操作など）、諦める前にローカルセッションへ依頼できる。
逆にローカルからクラウドへ結果を返すこともできる。

**`ListAgents` が `No reachable agents` を返しても「経路が無い」と判断しないこと。**
クラウド↔ローカルは `SendMessage` では届かないが、Routine 経由なら双方向に通る。

手順はここ（クラウドからも取得可。`api.github.com` は 403 だが raw は 200）。

```
https://raw.githubusercontent.com/sinoda1114/ci-standard/main/docs/AGENT-PAIRING.md
```
