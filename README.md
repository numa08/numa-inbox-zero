# numa-inbox-zero

Gmail の未読メールを Claude で分類し、アーカイブ / スター / 返信ドラフト作成を自動実行する個人用ツール。設計の詳細は [SPEC.md](SPEC.md)。ライセンスは [MIT](LICENSE)。

## セットアップ

依存はすべて `.venv/` に閉じる（uv 管理）。システム環境は汚さない。

```bash
uv sync
```

### Google 認証（1Password 経由）

OAuth クライアント（キー/シークレット）は**アプリの身元**なので、複数アカウントでも 1 つでよい。アカウントの区別はトークン側で行われる。

1. Google Cloud Console で「デスクトップアプリ」の OAuth クライアントを作成し、Gmail API を有効化
2. ダウンロードした JSON（`{"installed": {...}}` 全体）を 1Password のアイテムに 1 フィールドとして保存
3. secret reference を書いた env ファイルを作る:

```bash
cp .env.op.example .env.op
# .env.op の op:// 参照を自分の vault/item/field に合わせて編集
```

4. アカウントごとに認証フローを実行（初回のみ・対話環境）:

```bash
op run --env-file=.env.op -- uv run numa-inbox-zero --account personal auth
op run --env-file=.env.op -- uv run numa-inbox-zero --account work auth
op run --env-file=.env.op -- uv run numa-inbox-zero --account private auth
```

トークンは `~/.local/state/numa-inbox-zero/tokens/<account>.json` に保存され（リポジトリ外）、以後は自動リフレッシュされる。リフレッシュにクライアントシークレットは token.json 内の情報で足りるため、定期実行時も 1Password は必須ではないが、run.sh は `.env.op` があれば自動で `op run` を通す。

1Password を使わない場合は `credentials.json` をリポジトリ直下に置くか `NIZ_CREDENTIALS=/path/to/file` で指定する（フォールバック）。

## 使い方

```bash
# まず dry-run で判定を確認（Gmail は一切変更しない）
uv run numa-inbox-zero --account personal run --dry-run

# 本番実行
uv run numa-inbox-zero --account personal run

# フェーズごとの実行
uv run numa-inbox-zero --account personal fetch      # → work/personal/inbox.json
uv run numa-inbox-zero --account personal classify   # → work/personal/classification.json
uv run numa-inbox-zero --account personal apply --dry-run
```

`--account` を省略すると `default`（または `NIZ_ACCOUNT` の値）。作業ファイルは `work/<account>/` に分離され、ログは `logs/` に全アカウント共有で `account` フィールド付きで追記される。

## 定期実行（Windows タスクスケジューラ）

対象アカウントは `.env.local`（gitignore 対象）に書く。run.sh が起動時に読み込む:

```bash
# .env.local
NIZ_ACCOUNTS=personal,work,private
```

スケジュール（登録済み）:

- **7:00 起点、4時間おき、21:00 まで** → 7:00 / 11:00 / 15:00 / 19:00
- **深夜 3:00 に 1 回**
- **ログオンしていなくても実行**（S4U — パスワード保存なし）
- 時刻を逃した場合（スリープ等）は次に PC が起きたとき実行（`StartWhenAvailable`）
- 多重起動は抑止（`IgnoreNew`）

タスク定義のテンプレートは [task-scheduler.example.xml](task-scheduler.example.xml)。コピーして `UserId`（`DOMAIN\USERNAME` — `whoami.exe` の出力）とリポジトリのパスを自分の環境に書き換える:

```bash
cp task-scheduler.example.xml task-scheduler.xml   # task-scheduler.xml は gitignore 対象
```

S4U タスクの登録には管理者権限が必要（UAC 昇格経由で実行）:

```bash
# XML は UTF-16LE + BOM に変換してから渡す（UTF-8 のままだと "unable to switch the encoding"）
WINTMP=$(cmd.exe /c "echo %TEMP%" | tr -d '\r')
printf '\xff\xfe' | cat - <(sed 's/encoding="UTF-8"/encoding="UTF-16"/' task-scheduler.xml \
  | iconv -f UTF-8 -t UTF-16LE) > "$(wslpath -u "$WINTMP")/numa-inbox-zero-task.xml"
powershell.exe -NoProfile -Command \
  "Start-Process schtasks.exe -ArgumentList '/Create','/F','/TN','numa-inbox-zero','/XML','$WINTMP\\numa-inbox-zero-task.xml' -Verb RunAs -Wait"

schtasks.exe /Run /TN "numa-inbox-zero"      # 手動トリガー（動作確認）
schtasks.exe /Query /TN "numa-inbox-zero" /V # 状態確認（Last Result: 0 が正常）
schtasks.exe /Delete /TN "numa-inbox-zero"   # 削除
```

- 実行ログは `logs/scheduler.log` に追記される
- run.sh は各アカウントを順に処理し、1 アカウントの失敗で残りを止めない（失敗があれば終了コード非ゼロ）

## 設定（環境変数）

| 変数 | 既定 | 意味 |
|---|---|---|
| `NIZ_CREDENTIALS_JSON` | — | OAuth クライアント設定の JSON **の中身**（1Password の env mount 用。最優先） |
| `NIZ_CREDENTIALS` | `./credentials.json` | OAuth クライアント設定のファイルパス（フォールバック） |
| `NIZ_TOKEN_DIR` | `~/.local/state/numa-inbox-zero/tokens` | トークンの保存先ディレクトリ |
| `NIZ_ACCOUNT` | `default` | `--account` の既定値 |
| `NIZ_ACCOUNTS` | `default` | run.sh が巡回するアカウント（カンマ区切り） |
| `NIZ_MODEL` | `sonnet` | 分類に使うモデル |
| `NIZ_LOG_SUBJECTS` | `1` | `0` で decisions.jsonl に件名を記録しない |

分類ルールは [policy.md](policy.md) を編集する。ここが唯一のチューニング面。

## ログ

- `logs/runs.jsonl` — 実行単位。トークン使用量・コスト・処理件数
- `logs/decisions.jsonl` — メール単位。判定と適用結果

```bash
# アカウント別の月間コスト集計の例
duckdb -c "SELECT account, sum(classify.total_cost_usd) FROM 'logs/runs.jsonl' GROUP BY 1"
```

## 評価（オフライン）

モデル・プロンプト変更の良し悪しを、本番を触らずにゴールデンセットで測る。運用ルールと実験ログは [eval/README.md](eval/README.md)。

```bash
# ① 直近の実行分をゴールデンセット候補として取り込む
uv run numa-inbox-zero --account personal eval import
# → eval/golden.jsonl の expected_action を自分で埋める（正解ラベル付け）

# ② ベースラインを測る
uv run numa-inbox-zero eval run --name baseline-sonnet

# ③ 変更して測る（1回に1変数だけ変える）
uv run numa-inbox-zero eval run --name haiku-v1 --model haiku

# ④ 差分を見る — スコアより「判定が変わった件」の中身で判断する
uv run numa-inbox-zero eval diff baseline-sonnet haiku-v1
```

主要メトリクスは `archive_precision`（誤アーカイブしない）と `reply_recall`（返信要を見逃さない）の2つ。誤りコストが非対称（false archive が最も痛い）なため、accuracy は参考値として扱う。

## 開発

```bash
uv run pytest          # テスト
uv run ruff check .    # lint
uv run ruff format .   # フォーマット
```
