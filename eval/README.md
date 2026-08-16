# 評価（オフライン）

ワークフローの詳細は [../README.md](../README.md) の「評価」節を参照。
`golden.jsonl` と `results/` は実メール本文を含むため gitignore されている。この README（実験ログ）だけをコミットする。

## 運用ルール

- ラベル付けは「誤判定だけ `expected_action` を上書きする」方式。**null のままの行は
  `system_action` に同意として採点される**ため、`eval import` の直後に必ず全件レビューする
  （レビューしていない null は「未確認」ではなく「同意」と扱われてしまう）
- golden.jsonl は**追記のみ**。ラベル付け（expected_action の記入）以外で
  過去のエントリを書き換えない（実験の比較可能性を保つ）
- 1 実験で変えるのは **1 変数だけ**（モデル or ポリシー or プロンプト）
- 合否ゲート:
  - `archive_precision`: ベースラインから**低下不可**（false archive は実害）
  - `reply_recall`: -0.03 まで許容
  - `accuracy`: 参考値。合否には使わない

## 実験ログ

| 日付 | 実験名 | 変更 | archive_prec | reply_recall | cost | 判断 |
|---|---|---|---|---|---|---|
| 2026-08-06 | baseline-sonnet | なし（初期 policy、golden 80 件） | 0.7143 | -（reply 0 件） | $0.96 | ベースライン |
| 2026-08-06 | policy-v2-notifications | policy.md: 取引通知・チケット購入・イベント案内・CI 失敗・サービス終了を star に明記、カレンダー出欠応答・ダイジェスト通知を archive に明記 | 0.9111 | -（reply 0 件） | $1.01 | 採用（archive_prec +0.20、危険側の star→archive が 18→4 件に減少） |
| 2026-08-06 | haiku45-policy-v2 | モデル: sonnet → haiku 4.5 | 0.9355 | -（reply 0 件） | $0.36 | 保留（ゲートは通るが配送通知・領収書の過剰 star が約+10件/80、判定欠落1件。コスト1/3は魅力、ピン止めのノイズ次第） |
| 2026-08-06 | opus5-policy-v2 | モデル: sonnet → opus 5 | 0.9302 | -（reply 0 件） | $1.39 | 不採用（accuracy 同値・変化4件のみでコスト+38%に見合わない） |
| 2026-08-06 | policy-v3-delivery | policy.md: 配送状況通知（ヤマト等）を archive に明記。golden はヤマト1件のラベル修正あり（star→archive） | 0.9556 | -（reply 0 件） | $1.03 | 採用（accuracy 0.90、注文確認・領収書の過剰 star も解消。Render 通知 2 件が star に揺れた点は次回観察） |
| 2026-08-06 | haiku45-policy-v3 | モデル: sonnet → haiku 4.5（policy v3） | 0.8269 | -（reply 0 件） | $0.33 | 不採用（取引通知・削除通知の誤 archive 9 件。本文が HTML 代替文のみのメールを字義どおり読んで誤判定する癖を確認） |
| 2026-08-06 | haiku45-policy-v4 | policy.md: 本文不全時は件名で判断・削除予告も star を追記（haiku で測定） | 0.9070 | -（reply 0 件） | $0.35 | 不採用（危険側 9→4 件まで改善したがゲート未達。削除完了通知を「対応不要」と archive する癖が policy の明記でも直らず） |
| 2026-08-06 | sonnet-policy-v4 | 同 policy v4 を sonnet で測定 | 0.9286 | -（reply 0 件） | $1.01 | 不採用・v3 に戻す（OTP 通知の archive/missing 化、領収書の過剰 star 再発。「件名で star」例示が star バイアスとして作用） |
| 2026-08-06 | sonnet-v3-htmlbody | mail.py: 本文抽出を text/html 優先に変更（golden の本文 52 件を再取得。policy は v3 のまま） | 0.9556 | -（reply 0 件） | $0.99 | 採用（sonnet は完全に無風。切り詰め対象 23→15 件でコスト微減） |
| 2026-08-06 | haiku45-v3-htmlbody | 同抽出変更を haiku で測定 | 0.9070 | -（reply 0 件） | $0.35 | haiku 移行は引き続き見送り（policy を汚さず 0.8269→0.9070 まで改善。残る危険側 4 件は golden のラベルノイズ含みで、次バッチのラベル整理後に再判定） |
