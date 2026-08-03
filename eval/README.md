# 評価（オフライン）

ワークフローの詳細は [../README.md](../README.md) の「評価」節を参照。
`golden.jsonl` と `results/` は実メール本文を含むため gitignore されている。この README（実験ログ）だけをコミットする。

## 運用ルール

- golden.jsonl は**追記のみ**。過去のエントリを書き換えない（実験の比較可能性を保つ）
- 1 実験で変えるのは **1 変数だけ**（モデル or ポリシー or プロンプト）
- 合否ゲート:
  - `archive_precision`: ベースラインから**低下不可**（false archive は実害）
  - `reply_recall`: -0.03 まで許容
  - `accuracy`: 参考値。合否には使わない

## 実験ログ

| 日付 | 実験名 | 変更 | archive_prec | reply_recall | cost | 判断 |
|---|---|---|---|---|---|---|
| | | | | | | |
