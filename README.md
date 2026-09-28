# numa-inbox-zero

Gmail の受信トレイの未処理メールを [Jev](https://typesafe.ai)（TypeSafe の System One モデル）で分類し、アーカイブ / スターを自動適用する個人用ツール。設計の詳細は [SPEC.md](SPEC.md)。ライセンスは [MIT](LICENSE)。

判定は「読む必要がない → archive」「自分の目で確認が必要 → star」の 2 値。確信が足りないもの・本文が自動処理系に指示を出そうとしているものは archive せず star に倒す。メールの送信・下書き作成は一切行わない。

## セットアップ

依存はすべて `.venv/` に閉じる（uv 管理）。システム環境は汚さない。

```bash
uv sync
```

### TypeSafe（Jev）の API キー

[console.typesafe.ai](https://console.typesafe.ai/) でキーを発行し、1Password のアイテムに保存して `.env.op` の `NIZ_TYPESAFE_API_KEY` に secret reference を書く。SDK 標準の `TYPESAFE_API_KEY` でも読める。

**`.env.op` が無いと `run.sh` はキーを注入しない。** run.sh は `.env.op` があるときだけ `op run` を通すため、ファイルが無いと分類が毎サイクル失敗する（`logs/scheduler.log` に「分類を実行できない」が残る）。`cp .env.op.example .env.op` を忘れないこと。

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

# 常駐ポーリング（NIZ_ACCOUNTS の全アカウントを 5 分間隔で巡回）
./run.sh daemon
./run.sh daemon --interval 60 --dry-run   # 間隔変更・dry-run も可
```

`--account` を省略すると `default`（または `NIZ_ACCOUNT` の値）。作業ファイルは `work/<account>/` に分離され、ログは `logs/` に全アカウント共有で `account` フィールド付きで追記される。

## 定期実行

対象アカウントは `.env.local`（gitignore 対象）に書く。run.sh が起動時に読み込む:

```bash
# .env.local
NIZ_ACCOUNTS=personal,work,private
```

実行形態は 2 つ。現在はタスクスケジューラから常駐ポーリング（daemon）を起動する構成で登録している。

### 常駐ポーリング（daemon・登録済み）

```bash
./run.sh daemon
```

プロセスを立ち上げたまま `NIZ_POLL_INTERVAL`（既定 300 秒）ごとに fetch → classify → apply を全アカウントに対して繰り返す。新着ゼロのサイクルは API を呼ばず、runs.jsonl にも記録しない。API 障害等で 1 サイクルが失敗してもプロセスは死なず、次のポーリングで再試行する。

タスクスケジューラの登録内容:

- **ログオン時トリガー**で `run.sh daemon` を起動し常駐させる
- **バックグラウンド実行**（S4U — パスワード保存なし、コンソールウィンドウなし）
- **実行時間制限なし**（制限があると常駐プロセスが途中で kill される）
- 異常終了時は 1 分後に再起動（最大 3 回）、多重起動は抑止（`IgnoreNew`）
- 登録直後やタスク停止後に今すぐ始めるには `schtasks.exe /Run /TN "numa-inbox-zero"`
- PC のスリープ中・ログオン前は動かない

### ワンショット

```bash
./run.sh   # NIZ_ACCOUNTS を 1 巡して終了
```

時刻トリガーで回していた旧方式。daemon 移行後も手動での単発実行やフォールバックとして残している。

### タスクの登録手順

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
- 1 アカウントの失敗で残りを止めない（daemon は次のポーリングで再試行、ワンショットは失敗があれば終了コード非ゼロ）

## 設定（環境変数）

| 変数 | 既定 | 意味 |
|---|---|---|
| `NIZ_CREDENTIALS_JSON` | — | OAuth クライアント設定の JSON **の中身**（1Password の env mount 用。最優先） |
| `NIZ_CREDENTIALS` | `./credentials.json` | OAuth クライアント設定のファイルパス（フォールバック） |
| `NIZ_TOKEN_DIR` | `~/.local/state/numa-inbox-zero/tokens` | トークンの保存先ディレクトリ |
| `NIZ_ACCOUNT` | `default` | `--account` の既定値 |
| `NIZ_ACCOUNTS` | `default` | run.sh / daemon が巡回するアカウント（カンマ区切り） |
| `NIZ_POLL_INTERVAL` | `300` | daemon のポーリング間隔（秒） |
| `NIZ_TYPESAFE_API_KEY` | — | TypeSafe の API キー（`TYPESAFE_API_KEY` でも可） |
| `NIZ_TYPESAFE_BASE_URL` | — | OpenRouter 等のゲートウェイ経由にする場合のみ |
| `NIZ_MODEL` | `jev-1.13.0` | 分類に使うモデル |
| `NIZ_CONCURRENCY` | `8` | 分類の同時リクエスト数 |
| `NIZ_LOG_SUBJECTS` | `1` | `0` で decisions.jsonl に件名を記録しない |

分類ルールは [policy.toml](policy.toml) を編集する。ここが唯一のチューニング面 — カテゴリ（Jev に渡す選択肢）と、それぞれを archive / star のどちらにするか、そして降格の閾値がここに集まっている。

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
# ① 実行ログ（decisions.jsonl）の判定をゴールデンセット候補として取り込む
#   （本文・件名はログに残らないため Gmail API から再取得して合成する）
uv run numa-inbox-zero --account personal eval import
# → eval/golden.jsonl を全件レビューし、誤判定だけ expected_action を上書きする
#   （null のままの行は system_action に同意として採点される）

# ② 測る
op run --env-file=.env.op -- uv run numa-inbox-zero eval run --name jev-v1

# ③ 閾値を選ぶ（保存済みの結果に対する掃引。API は呼ばない = 追加コスト 0）
uv run numa-inbox-zero eval sweep jev-v1

# ④ 変更して測る（1回に1変数だけ変える）
op run --env-file=.env.op -- uv run numa-inbox-zero eval run --name jev-v2

# ⑤ 差分を見る — スコアより「判定が変わった件」の中身で判断する
uv run numa-inbox-zero eval diff jev-v1 jev-v2
```

主要メトリクスは `archive_precision`（誤アーカイブしない）と `archive_recall`（受信トレイを空にできている）の 2 つ。誤りコストが非対称（false archive が最も痛い）なため、accuracy は参考値として扱う。ゴールデンセットの正解の大半は過去のモデルの判定なので、人間が明示的にラベルした行の正答数（`human_labeled`）を併せて見る。

## 開発

```bash
uv run pytest          # テスト
uv run ruff check .    # lint
uv run ruff format .   # フォーマット
```
