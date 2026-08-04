#!/usr/bin/env bash
# Windows タスクスケジューラから wsl.exe 経由で呼ばれるエントリポイント。
#
# - .env.local があれば読み込む（NIZ_ACCOUNTS 等のローカル設定。gitignore 対象）
# - NIZ_ACCOUNTS（カンマ区切り）の各アカウントを順に処理する。未設定なら "default"。
# - `run.sh daemon [...]` は常駐ポーリングモード。アカウント巡回は Python 側が
#   NIZ_ACCOUNTS を読んで行うため、ここでは巡回しない。
# - .env.op があり op CLI が使えるなら、1Password から NIZ_CREDENTIALS_JSON を注入する。
#   （定期実行ではトークンリフレッシュに不要だが、あれば通しておく）
# - 出力は logs/scheduler.log にも追記する。無人実行の失敗を後から追うため。
# - 1 アカウントの失敗で残りを止めない。ただし失敗があれば終了コードは非ゼロ。
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")"

# タスクスケジューラ経由だとログイン シェルの PATH が乗らないことがある
export PATH="$HOME/.local/bin:$PATH"

# stdout が tee へのパイプになるため、バッファリングさせると daemon のログが
# scheduler.log に反映されるまで大きく遅延する
export PYTHONUNBUFFERED=1

if [[ -f .env.local ]]; then
  # set -a で export 込みで読み込む。NIZ_ACCOUNTS 等は daemon（Python 側）が
  # 環境変数として読むため、シェル変数のままでは伝わらない
  set -a
  # shellcheck disable=SC1091
  source .env.local
  set +a
fi

mkdir -p logs
exec > >(tee -a logs/scheduler.log) 2>&1
echo "--- $(date -Iseconds) run.sh start ---"

RUNNER=(uv run numa-inbox-zero)
if [[ -f .env.op ]] && command -v op >/dev/null 2>&1; then
  RUNNER=(op run --env-file=.env.op --no-masking -- uv run numa-inbox-zero)
fi

if [[ "${1:-}" == "daemon" ]]; then
  shift
  exec "${RUNNER[@]}" daemon "$@"
fi

ACCOUNTS="${NIZ_ACCOUNTS:-default}"
failed=0
for account in ${ACCOUNTS//,/ }; do
  echo "=== account: ${account} ==="
  if ! "${RUNNER[@]}" --account "$account" run "$@"; then
    echo "account ${account} の実行に失敗" >&2
    failed=1
  fi
done

echo "--- $(date -Iseconds) run.sh end (failed=${failed}) ---"
exit "$failed"
