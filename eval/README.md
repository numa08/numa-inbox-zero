# 評価（オフライン）

ワークフローの詳細は [../README.md](../README.md) の「評価」節を参照。
`golden.jsonl` と `results/` は実メール本文を含むため gitignore されている。この README（実験ログ）だけをコミットする。

## 運用ルール

- ラベル付けは「誤判定だけ `expected_action` を上書きする」方式。**null のままの行は
  `system_action` に同意として採点される**ため、`eval import` の直後に必ず全件レビューする
  （レビューしていない null は「未確認」ではなく「同意」と扱われてしまう）
- golden.jsonl は**追記のみ**。ラベル付け（expected_action の記入）以外で
  過去のエントリを書き換えない（実験の比較可能性を保つ）
- 1 実験で変えるのは **1 変数だけ**（モデル or カテゴリ定義 or 質問文 or 本文切り詰め幅）
- 合否ゲート:
  - `archive_precision`: ベースラインから**低下不可**（false archive は実害）
  - `archive_recall`: -0.03 まで許容（受信トレイを空にできている度合い）
  - `human_labeled`: 人間が明示的にラベルした行の正答数。**下がったら理由を確認する**
  - `accuracy`: 参考値。合否には使わない

**このゴールデンセットの正解の大半は「過去のモデルの判定」である。** `expected_action` が null の行は
当時の判定（`system_action`、初期は claude Sonnet 5）への同意として採点されるため、
`archive_precision: 0.95` は「独立した正解に対して 95% 正しい」ではなく
「旧モデルの判定と 95% 一致している」に近い。人間が明示的に上書きした行だけが独立した信号なので、
`eval run` はその部分集合（`human_labeled`）を別途集計して報告する。ラベルを増やすほどこのゲートは強くなる。

### 閾値の選び方

`archive_confidence` の掃引は `eval sweep <実験名>` で行う。保存済みの結果ファイルに残した生の
`confidence` を読み直すだけなので **API を呼ばず追加コストが 0** で、何度でもやり直せる。
`false_arch`（本来 star のメールをアーカイブした件数）を 0 に保てる最小の閾値を選び、
`policy.toml` の `[thresholds]` に書く。掃引のゲート計算は本番と同じ `validate.gate_archive` を
呼んでいるため、掃引で見た数字がそのまま本番の挙動になる。

## 実験ログ

2026-09-28 の Jev 移行より前の行は claude CLI バックエンド（生成モデル + JSON スキーマ）での測定で、
`reply_recall` 列は当時の 2 つ目のゲート（返信要の取りこぼし）。ゴールデンセットに reply の正解が
1 件も無かったため一度も機能せず、reply アクションの廃止に伴い `archive_recall` に置き換えた。
`baseline-sonnet` 以降の結果ファイルは同じ golden セットなので、Jev の結果と直接 `eval diff` できる。

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
| 2026-09-28 | （未測定） | バックエンド: claude CLI → Jev（`jev-1.13.0`）、reply 廃止、policy.md → policy.toml のカテゴリ方式、archive を confidence 0.85 + injection ゲートに変更 | — | — | — | **API キー発行後に `eval run --name jev-v1` で測定 → `eval sweep` で閾値決定 → `eval diff sonnet-v3-htmlbody jev-v1`。日本語での精度は未検証（公式に CJK は英語より劣ると明記）** |
