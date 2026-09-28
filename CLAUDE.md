# CLAUDE.md

Gmail の受信トレイの未処理メールを Jev（TypeSafe の System One モデル）で分類し、
アーカイブ / スターを付ける個人用ツール。
設計の全体像と根拠は [SPEC.md](SPEC.md)、利用手順は [README.md](README.md) を先に読むこと。

## コマンド

```bash
uv sync                  # 依存関係の同期（.venv/ に閉じる。システムを汚さない）
uv run pytest            # テスト実行
uv run ruff check src tests    # lint
uv run ruff format src tests   # フォーマット
uv run numa-inbox-zero --help  # CLI
```

TypeSafe の API キーは `NIZ_TYPESAFE_API_KEY`（1Password の env mount）で注入する。
API を呼ぶコマンド（`classify` / `run` / `daemon` / `eval run`）は `op run --env-file=.env.op -- ...` 経由で実行する。

- パッケージ管理は **uv のみ**。pip / poetry を直接使わない
- コミット前に `ruff check` と `pytest` が通ること
- lint エラーは suppress（`# noqa`）せず修正する。どうしても必要な場合は理由をコメントに書く

## ファイル構成

```
src/numa_inbox_zero/
├── __main__.py   # CLI（auth / fetch / classify / apply / run / daemon / eval）。フェーズの編成のみ
├── config.py     # 設定・パス解決。環境変数（NIZ_*）の読み取りはここに集約
├── mail.py       # 純粋な変換処理（本文抽出・ヘッダ処理）。ネットワーク禁止
├── gmail.py      # Gmail API ラッパ。Gmail に触るのはこのモジュールだけ
├── policy.py     # policy.toml の読み込みと検証。純粋
├── classify.py   # Jev の呼び出し（1 通 = 1 リクエスト）と判定の組み立て
├── validate.py   # 分類結果の検証と archive ゲート。セキュリティの砦
├── apply.py      # 検証済み判定の Gmail への適用
├── runlog.py     # JSONL ログ（runs.jsonl / decisions.jsonl）
└── evaluate.py   # オフライン評価（eval import / run / sweep / diff）の純粋ロジック

policy.toml             # カテゴリ定義（Jev の Choice の criteria）と閾値。唯一のチューニング面
tests/                  # pytest。モジュール単位で test_<module>.py
work/<account>/         # 実行時の中間ファイル（gitignore）
logs/                   # runs.jsonl / decisions.jsonl / scheduler.log（gitignore）
eval/                   # golden.jsonl / results/（gitignore）。README.md のみコミット対象
```

Jev にはシステムプロンプトも出力スキーマ指定も無い。指示は questions の `instructions` /
`criteria` がすべてで、型は API が保証する。そのため `prompts/` と `schemas/` は存在しない。

### モジュール境界のルール

- **ネットワークを触るコードは `gmail.py` と `classify.py` だけ**。他モジュールに漏らさない
- 純粋な変換処理は `mail.py` / `policy.py` / `evaluate.py` / `validate.py` に置き、オフラインでテスト可能に保つ
- 環境変数の読み取りは `config.py` に集約。他モジュールで `os.environ` を直接読まない
  （`typesafe-sdk` は `TYPESAFE_API_KEY` を自分で読めるが、それに頼らず明示的に渡す）
- `action`（archive / star）を決めるのはコードだけ。モデルにはカテゴリしか選ばせない

## テストルール

- フレームワークは pytest。配置は `tests/test_<module>.py`、クラスで機能単位にグルーピング
- **テスト名は日本語**で「何を保証するか」を書く（例: `test_捏造message_idはdry_runでもrejectedになる`）
- **テストはネットワークに出ない**。Gmail API は `unittest.mock.patch`、TypeSafe API は
  `system_one` だけを持つスタブクライアント（`classify_messages(client=...)`）で差し替える。
  レスポンスは SDK の実型（`SystemOneResponse.model_validate`）に通してから解釈させ、
  型の変更に気づけるようにする
- 新しい検証・降格・ログのロジックには必ずテストを付ける。特に以下の性質は回帰させない:
  - 入力に存在しない `message_id` は実行されない
  - enum 外の action（`send` / `reply` 等）は拒否される
  - policy.toml に無いカテゴリが返ったら何も実行されない
  - `confidence` が閾値未満の archive は star に降格される（欠損・不正値・範囲外も含む）
  - injection の Noul が閾値を超えた archive は star に降格される
  - `state` に message_id・宛先・メールアドレスのローカル部が入らない
  - 下書き・送信系の Gmail API 関数がコードベースに存在しない
  - dry-run では Gmail API が一切呼ばれない
  - ログにメールアドレス全体・本文が残らない（ドメインと文字数のみ）
- テスト失敗を skip / コメントアウトで握りつぶさない

## セキュリティ不変条件（変更禁止）

このシステムはメール本文（信用できない外部入力）を LLM に渡し、その出力で Gmail を操作する。
以下はプロンプトインジェクション対策の構造であり、緩めてはならない:

1. **分類は 1 通 = 1 リクエスト**に保つ。`message_id` をモデルに渡さず（`state` に入れない）、
   コード側が保持したまま結果と突き合わせる。まとめて渡す実装に戻すと「モデルの出力が
   操作対象のメールを選べる」経路が復活する
2. 分類器の出力は `validate.py` を必ず通す。message_id の照合・action の enum 検証を省略しない
3. **archive は不可逆側**。`confidence` と `injection` のゲート（`validate.gate_archive`）を
   緩めない。Jev は state を敵対的入力として扱わないと公式に明言されているため、
   本文に誘導されうる前提でコード側が止める。
   ただし `injection` の Noul 自体も同じ弱点を持つモデルの判定であり、
   **日本語のインジェクション試行に対して実際に高い値を返すかは未測定**。
   多重防御の 1 枚として扱い、これ単独を根拠に他のゲートを緩めない
   （ゴールデンセットにインジェクション試行の行を足して測るのが次の一手）
4. **送信・下書き API（`users.messages.send` / `users.drafts.send` / `users.drafts.create`）を
   コードベースに追加しない**
5. OAuth スコープは `gmail.modify` のみ。スコープを追加しない
6. 認証情報をコード・ログ・リポジトリに置かない。クライアント設定は `NIZ_CREDENTIALS_JSON`、
   TypeSafe の API キーは `NIZ_TYPESAFE_API_KEY`（どちらも 1Password env mount）、
   トークンは XDG state（リポジトリ外）

## ログと個人情報

- `decisions.jsonl` に記録するのは from の**ドメインのみ**・件名（`NIZ_LOG_SUBJECTS=0` でオフ）・
  本文の**文字数のみ**。メールアドレス全体と本文は記録しない
- OSS リポジトリのため、ドキュメント・コード・テストに実在のメールアドレス・アカウント名・
  ユーザー名・絶対パスを書かない。例示は `you@example.com` / `personal,work,private` /
  `/path/to/numa-inbox-zero` を使う
- 実値が必要なローカルファイル（`.env.local` / `task-scheduler.xml` 等）は gitignore 済み。
  解除しない

## 変更時の作法

- 分類の挙動を変えるときは `policy.toml`（カテゴリと閾値）→ `classify.py` の `instructions`（質問文）の
  順で検討する。コードでの対応は検証・ゲートの変更が必要なときだけ
- カテゴリを増やすコストは数トークン。粒度を細かく保ち、`other` に落ちる件数が増えたら追加を検討する
- Jev は書いた条件を字義どおりに読む。誤答を見て「本当はこう言いたかった」と説明したくなったら、
  その説明が `description` に足りていない半分
- モデル・カテゴリ・質問文の変更は本番投入前に `eval run` で採点し、`eval diff` でベースラインと
  比較する（運用ルールは [eval/README.md](eval/README.md)）。合否は `archive_precision`（低下不可）と
  `archive_recall`（-0.03 まで）で判断し、人間がラベルした行の正答数（`human_labeled`）も見る
- 閾値のチューニングは `eval sweep`（保存済み結果に対する掃引・API 不要）で行う。
  掃引のために結果ファイルに残している生の `confidence` / `injection` を削らない
- `NIZ_MODEL` の既定はバージョン固定（`jev-1.13.0`）。confidence の閾値はモデルバージョンごとに
  較正された値なので、`jev-latest` のような alias を既定にしない。モデルを上げたら閾値を再確認する
- Gmail API / TypeSafe API の呼び出し形を変えるときは Context7 か公式ドキュメント
  （docs.typesafe.ai）を確認してから実装する
