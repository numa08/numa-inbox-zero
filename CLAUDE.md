# CLAUDE.md

Gmail の受信トレイの未処理メールを `claude -p` で分類し、アーカイブ / スター / 返信ドラフト作成を行う個人用ツール。
設計の全体像と根拠は [SPEC.md](SPEC.md)、利用手順は [README.md](README.md) を先に読むこと。

## コマンド

```bash
uv sync                  # 依存関係の同期（.venv/ に閉じる。システムを汚さない）
uv run pytest            # テスト実行
uv run ruff check src tests    # lint
uv run ruff format src tests   # フォーマット
uv run numa-inbox-zero --help  # CLI
```

- パッケージ管理は **uv のみ**。pip / poetry を直接使わない
- コミット前に `ruff check` と `pytest` が通ること
- lint エラーは suppress（`# noqa`）せず修正する。どうしても必要な場合は理由をコメントに書く

## ファイル構成

```
src/numa_inbox_zero/
├── __main__.py   # CLI（auth / fetch / classify / apply / run / eval）。フェーズの編成のみ
├── config.py     # 設定・パス解決。環境変数（NIZ_*）の読み取りはここに集約
├── mail.py       # 純粋な変換処理（本文抽出・MIME 構築・ヘッダ処理）。ネットワーク禁止
├── gmail.py      # Gmail API ラッパ。ネットワークを触るのはこのモジュールだけ
├── classify.py   # claude -p の起動と出力パース。将来 Anthropic API に差し替える境界
├── validate.py   # 分類結果の検証と confidence 降格。セキュリティの砦
├── apply.py      # 検証済み判定の Gmail への適用
├── runlog.py     # JSONL ログ（runs.jsonl / decisions.jsonl）
└── evaluate.py   # オフライン評価（eval import / run / diff）の純粋ロジック

prompts/classifier.md   # 分類器のシステムプロンプト（セキュリティ規則を含む）
policy.md               # 分類ルール。唯一のチューニング面
schemas/classification.json  # 分類出力の JSON スキーマ
tests/                  # pytest。モジュール単位で test_<module>.py
work/<account>/         # 実行時の中間ファイル（gitignore）
logs/                   # runs.jsonl / decisions.jsonl / scheduler.log（gitignore）
eval/                   # golden.jsonl / results/（gitignore）。README.md のみコミット対象
```

### モジュール境界のルール

- **ネットワークを触るコードは `gmail.py` と `classify.py`（subprocess）だけ**。他モジュールに漏らさない
- 純粋な変換処理は `mail.py` / `evaluate.py` / `validate.py` に置き、オフラインでテスト可能に保つ
- 環境変数の読み取りは `config.py` に集約。他モジュールで `os.environ` を直接読まない

## テストルール

- フレームワークは pytest。配置は `tests/test_<module>.py`、クラスで機能単位にグルーピング
- **テスト名は日本語**で「何を保証するか」を書く（例: `test_捏造message_idはdry_runでもrejectedになる`）
- **テストはネットワークに出ない**。Gmail API は `unittest.mock.patch`、claude CLI は `ClassifyOutcome` のスタブで差し替える
- 新しい検証・降格・ログのロジックには必ずテストを付ける。特に以下の性質は回帰させない:
  - 入力に存在しない `message_id` は実行されない
  - enum 外の action（`send` 等）は拒否される
  - `confidence != "high"` の archive は star に降格される（欠損・不正値も含む）
  - reply はラベル付与 → ドラフト作成の順で呼ばれる
  - dry-run では Gmail API が一切呼ばれない
  - ログにメールアドレス全体・本文が残らない（ドメインと文字数のみ）
- テスト失敗を skip / コメントアウトで握りつぶさない

## セキュリティ不変条件（変更禁止）

このシステムはメール本文（信用できない外部入力）を LLM に渡し、その出力で Gmail を操作する。
以下はプロンプトインジェクション対策の構造であり、緩めてはならない:

1. `claude -p` は必ず隔離フラグ付きで起動する（`classify.py` の `CLAUDE_ISOLATION_FLAGS`）。
   `--tools ""` `--setting-sources ""` `--strict-mcp-config` を外さない
2. 分類器の出力は `validate.py` を必ず通す。message_id の照合・action の enum 検証を省略しない
3. **送信 API（`users.messages.send` / `users.drafts.send`）をコードベースに追加しない**。
   ドラフトは作成のみ
4. OAuth スコープは `gmail.modify` のみ。スコープを追加しない
5. 認証情報をコード・ログ・リポジトリに置かない。クライアント設定は `NIZ_CREDENTIALS_JSON`
   （1Password env mount）、トークンは XDG state（リポジトリ外）

## ログと個人情報

- `decisions.jsonl` に記録するのは from の**ドメインのみ**・件名（`NIZ_LOG_SUBJECTS=0` でオフ）・
  本文の**文字数のみ**。メールアドレス全体と本文は記録しない
- OSS リポジトリのため、ドキュメント・コード・テストに実在のメールアドレス・アカウント名・
  ユーザー名・絶対パスを書かない。例示は `you@example.com` / `personal,work,private` /
  `/path/to/numa-inbox-zero` を使う
- 実値が必要なローカルファイル（`.env.local` / `task-scheduler.xml` 等）は gitignore 済み。
  解除しない

## 変更時の作法

- 分類の挙動を変えるときは `policy.md`（ルール）→ `prompts/classifier.md`（構造）の順で検討する。
  コードでの対応はスキーマ・検証の変更が必要なときだけ
- モデル・プロンプト変更は本番投入前に `eval run` でゴールデンセットに対して採点し、
  `eval diff` でベースラインと比較する（運用ルールは [eval/README.md](eval/README.md)）。
  合否は `archive_precision`（低下不可）と `reply_recall`（-0.03 まで）で判断する
- Gmail API の呼び出し形を変えるときは Context7 でドキュメントを確認してから実装する
- claude CLI の出力パース（`classify.py`）はフィールド欠損を許容する方針を維持する
  （CLI バージョンで構造が変わるため）
