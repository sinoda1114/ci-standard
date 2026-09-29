# agent pairing の疎通テスト手順

新しいプロジェクトでクラウド↔ローカルの往復を初めて成立させるときの手順。
**手順そのものは [AGENT-PAIRING.md](./AGENT-PAIRING.md) が正本。** ここはそれを
新規プロジェクトで一度通し、動いていることを客観的に確認するための検査票。

所要 10〜15 分。クラウド側が主導し、ローカル側には T4 の1回だけ作業が発生する。

---

## 先に合格条件を決める

**すべて満たしたときだけ「疎通した」と言う。** 一つでも欠けたら未達として扱う。

| # | 合格条件 | 判定方法 |
|---|---|---|
| A | クラウドが許可プロンプト無しで trigger を扱える | `list_triggers` がプロンプト無しで返る |
| B | 双方が相手を一意に特定できる | `pair:<repo>` タグを持つ行が自分と相手のちょうど2件 |
| C | クラウド→ローカルで**本文が**届く | ローカルが本文を引用して返信できる |
| D | ローカル→クラウドで**本文が**届く | クラウドが本文を読める |
| E | 配送先が検証できる | 戻り値の `session_id` が `cse_<宛先 suffix>` と一致 |
| F | 既知の失敗モードが再現する | 発火時に本文を渡すと E が**不一致**になる |
| G | trigger が残らない | テスト後 `list_triggers` に `pair:<repo>` が0件 |

> **C と D は「届いた」だけでは不足。本文まで届いたかを見る。**
> 封筒だけ届いて本文が落ちるのが最も多い壊れ方で、しかも送信側は成功に見える。
> 相手に**本文中の特定の語を引用させる**ことで初めて確認できる。

F を入れているのは、失敗モードが新環境でも同じか確かめるため。
ここが再現しないなら仕様が変わった可能性があり、正本の見直しが要る。

---

## 前提

- 両セッションが**同一アカウント**にある（別アカウント間は不可）
- 両方が対象リポジトリを `session_context.sources` に持つ
- ローカルセッションが起動中（`connection_status: connected`）

対象リポジトリ名を決めておく。以下 `<repo>` と書く。

---

## T1. 許可設定を入れる（クラウド側・初回のみ）

**これを飛ばすと T3 以降で毎回プロンプトが出る。** ひどいときは分類器に
ブロックされて `list_triggers` すら通らない。

リポジトリに `.claude/settings.json` を置く。

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "permissions": {
    "allow": [
      "mcp__Claude_Code_Remote__list_sessions",
      "mcp__Claude_Code_Remote__get_session",
      "mcp__Claude_Code_Remote__set_session_tags",
      "mcp__Claude_Code_Remote__list_triggers",
      "mcp__Claude_Code_Remote__create_trigger",
      "mcp__Claude_Code_Remote__update_trigger",
      "mcp__Claude_Code_Remote__fire_trigger",
      "mcp__Claude_Code_Remote__delete_trigger",
      "mcp__Claude_Code_Remote__send_later"
    ]
  }
}
```

> **`~/.claude/settings.json` ではなくリポジトリに置く。** クラウドのコンテナは
> 毎回作り直され、ユーザー設定は存在しない。**main にマージするまで完了ではない。**
> ブランチに置いただけでは次のクラウドセッションで元に戻る。

**判定（合格条件 A）**

```
list_triggers(limit: 5)
```

プロンプトが出ずに返れば合格。出るなら main に入っていないか、パスが違う。

---

## T2. 相手を探してタグを付ける

```
list_sessions(mine: true, limit: 50)
```

> `tags` 引数はセッション内から呼ぶと `tags filter is not currently available` で
> 失敗する。戻り値の各行に `tags` は入っているので**クライアント側で絞る。**

| 相手 | 見分け方 |
|---|---|
| ローカル | `environment_kind: bridge` かつ `tags` に `remote-control-*` かつ `connection_status: connected` |
| クラウド | `environment_kind: anthropic_cloud`、`origin` が `web_claude_ai` / `desktop_app` |

`session_context.sources` に `<repo>` が入っていることも見る。
**候補が複数出たら推測せずユーザーに確認する。** 宛先を間違えると無関係な
セッションに割り込む。

```
set_session_tags(session_ids: ["<自分>"], add: ["pair:<repo>", "pair-role:cloud"])
set_session_tags(session_ids: ["<相手>"], add: ["pair:<repo>", "pair-role:local"])
```

**判定（合格条件 B）** — 再度 `list_sessions` して `pair:<repo>` が
ちょうど2件、`pair-role` が `cloud` と `local` で1件ずつ。

---

## T3. わざと失敗させる（負のコントロール）

**先に壊れ方を見ておく。** 正常系だけ通すと、次に壊れたとき原因を切り分けられない。

合言葉を決める。ここでは `SMOKE-ALPHA` とする。

```
create_trigger(
  name: "pair:<repo> / cloud→local #smoke-ng",
  persistent_session_id: "<相手>",
  prompt: "【疎通テスト】封筒のみ"
)
fire_trigger(trigger_id: "<上の id>", text: "合言葉は SMOKE-ALPHA です")
```

**判定（合格条件 F）** — 戻り値の `session_id` が宛先と**一致しないこと**。

```
宛先        session_01ABC...
戻り値      cse_01XYZ...      ← 別物なら想定どおり
```

新規の使い捨てセッションが起きており、相手には届いていません。
一致してしまった場合は仕様が変わった可能性があるので、正本を見直してください。

```
delete_trigger(trigger_id: "<上の id>")
```

---

## T4. クラウド→ローカル（正常系）

**封筒と本文をまとめて `prompt` に入れ、発火では `trigger_id` だけ渡す。**

```
create_trigger(
  name: "pair:<repo> / cloud→local #smoke-1",
  persistent_session_id: "<相手>",
  prompt: "<封筒>\n\n合言葉は SMOKE-BRAVO です。\n\nこのメッセージが本文まで届いたか確認しています。\n返信に合言葉をそのまま含めてください。あわせて、あなたが送信に使った\nツール名と、戻り値の session_id を書いてください。"
)
fire_trigger(trigger_id: "<上の id>")
```

封筒は [AGENT-PAIRING.md](./AGENT-PAIRING.md) の「封筒」節の文面をそのまま使う。

**判定**

| # | 見るもの | 合格 |
|---|---|---|
| 合格条件 E | 戻り値の `session_id` | `cse_<宛先の suffix>` と一致 |
| 合格条件 C | 返信の内容 | `SMOKE-BRAVO` が含まれる |

**`SMOKE-BRAVO` が返ってこなければ本文が落ちています。** 封筒だけ届いた状態で、
T3 と同じ壊れ方です。発火時に本文を渡していないか確認してください。

```
delete_trigger(trigger_id: "<上の id>")
```

---

## T5. ローカル→クラウド（逆方向）

T4 の返信が届いた時点で **合格条件 D は自動的に満たされます。** 返信そのものが
ローカル→クラウドの疎通だからです。追加の作業は要りません。

返信に含めてもらった内容で、次を確認します。

| 見るもの | 期待 |
|---|---|
| 送信に使ったツール | ローカルは `RemoteTrigger`（組み込み）。MCP ではない |
| 戻り値の `session_id` | クラウド側の id と一致していること |

> **ローカルには `delete` がありません。** 使えるのは list / get / create /
> update / run / create_webhook_trigger のみ。ローカルは `update` で
> `enabled: false` にするだけなので、**実体の回収はクラウド側の仕事です。**

---

## T6. 後片付け

```
list_triggers(limit: 50)
```

`pair:<repo>` を含む名前のものを**すべて** `delete_trigger` する。
ローカルが無効化しただけのもの（`enabled: false`）も対象です。

**判定（合格条件 G）** — `pair:<repo>` が0件。

> **名前を一意にしておくこと。** 1通ごとに作るので、同じ名前を使い回すと
> ここでどれを消すか識別できません。上の `#smoke-1` のような連番で十分です。

---

## 落とし穴

実際に踏んだものだけを挙げます。

| 症状 | 原因 | 対処 |
|---|---|---|
| 相手に届かない。エラーも出ない | 発火時に本文を渡した | `prompt` に入れる。T3 で再現を確認 |
| 自分自身に届いた | `persistent_session_id` に自分の id を入れた | **配送先**であって送信元ではない |
| 毎回3回プロンプトが出る | 許可設定が main に無い | T1。ブランチに置くだけでは不足 |
| `list_triggers` すら通らない | 同上 | 同上 |
| 後片付けでどれを消すか分からない | trigger 名が重複 | 名前に連番を付ける |
| `ListAgents` に相手が出ない | ピア層の話で Routine とは無関係 | **経路が無いと判断しない。** T2 へ進む |
| prompt を直したいが編集できない | 他セッション宛は `update_trigger` 不可 | 作り直す。最初から正しく作る |

> **`ListAgents` が `No reachable agents` を返しても諦めないこと。**
> これを「経路が無い」と読み違えて、ユーザーに手動でコピペ中継を頼んでしまった
> 実例があります。

---

## 補足: trigger と MCP コネクタ

`create_trigger` は呼び出し元セッションが持つコネクタしか引き継げません。
持っていない場合は次の警告が出ます。

```
this trigger stores no MCP connectors, so the sessions it fires will run
without connector (mcp__<server>__*) tools
```

**pairing の用途では無視してよい警告です。** 配送先は `persistent_session_id` で
指定した既存セッションであり、そのセッションは自分のツールをそのまま持っています。
警告が問題になるのは、発火のたびに新規セッションを起こす使い方のときだけです。
