"""実行時設定。

複数アカウント対応の考え方:
- OAuth クライアント（キー/シークレット）はアプリの身元なので全アカウント共通で 1 つ
- アカウントごとに分かれるのはトークン・作業ディレクトリ
- ログは全アカウント共有の JSONL に account フィールド付きで追記し、横断集計できるようにする

認証情報はリポジトリ内に置かない:
- クライアント設定は 1Password の env mount（NIZ_CREDENTIALS_JSON に JSON の中身）で注入
- トークンは XDG state（~/.local/state/numa-inbox-zero/）に保存
- Cloudflare の API トークンも env mount（NIZ_CF_API_TOKEN）で注入する
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 一度処理したメールに付けるラベル。取得クエリから除外することで再処理を防ぐ。
PROCESSED_LABEL = "numa-inbox-zero/processed"

# Gmail の検索クエリ。既読/未読は問わない（通知を開いただけで既読になるケースを
# 取りこぼさないため）。意図的に残すメールはスターで表現する運用とし、-is:starred で除外。
# newer_than は初回実行時の暴発防止
# （これより古いメールは永久にスキャン対象外になる点に注意 — SPEC.md §3）。
FETCH_QUERY = f"in:inbox -is:starred -label:{PROCESSED_LABEL} newer_than:7d"

MAX_MESSAGES_PER_RUN = 50

# daemon モードのポーリング間隔（秒）。短くするほど受信から処理までの遅延は減るが、
# 1 回のポーリングで分類するメールが減り、リクエスト数あたりの固定コストがかさむ。
DEFAULT_POLL_INTERVAL_SECONDS = 300

# 長文は Jev の精度も落とす（無関係な情報が多い state で精度が下がる）ため切り詰める。
# 短くするほど精度・コストの両方に効く可能性があり、`eval run` で検証する対象。
BODY_TRUNCATE_CHARS = 2000

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Workers AI での Jev のモデル ID。Cloudflare 側はバージョン付き ID を公開していないため、
# **閾値をバージョンに固定できない**（TypeSafe 直叩きの jev-1.13.0 とは違う点）。
# レスポンスの model は実際に答えたバージョン（jev-1.13.0 等）を返すので、
# runs.jsonl と eval 結果に記録して drift を検知する運用で埋める。
# 記録されたバージョンが変わったら policy.toml の閾値を eval sweep で再確認する。
DEFAULT_MODEL = "typesafe/jev"

# 分類の同時リクエスト数。ローカル実行では 8 で問題ないが、Workers 上では
# 「ヘッダ待ち 6 本」の同時接続制限があるため、移行時は 6 以下に下げる。
DEFAULT_CONCURRENCY = 8

DEFAULT_ACCOUNT = "default"


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def poll_interval_seconds() -> int:
    return int(os.environ.get("NIZ_POLL_INTERVAL", DEFAULT_POLL_INTERVAL_SECONDS))


def accounts_from_env() -> list[str]:
    """NIZ_ACCOUNTS（カンマ区切り）を分解する。未設定なら空リスト。"""
    raw = os.environ.get("NIZ_ACCOUNTS", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


@dataclass
class Config:
    account: str = DEFAULT_ACCOUNT
    root: Path = PROJECT_ROOT

    # OAuth クライアント設定。credentials_json（中身）が優先、無ければ credentials_path。
    credentials_json: str | None = field(
        default_factory=lambda: os.environ.get("NIZ_CREDENTIALS_JSON")
    )
    credentials_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("NIZ_CREDENTIALS", PROJECT_ROOT / "credentials.json")
        )
    )

    token_path: Path | None = None
    work_dir: Path | None = None
    logs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    policy_path: Path = field(default_factory=lambda: PROJECT_ROOT / "policy.toml")

    # Workers AI のモデル ID（既定 typesafe/jev）。
    model: str = field(default_factory=lambda: os.environ.get("NIZ_MODEL", DEFAULT_MODEL))
    # Cloudflare の認証情報。wrangler 等と環境を共有できるよう、CF 慣習の名前も受ける。
    cf_account_id: str | None = field(
        default_factory=lambda: (
            os.environ.get("NIZ_CF_ACCOUNT_ID") or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        )
    )
    cf_api_token: str | None = field(
        default_factory=lambda: (
            os.environ.get("NIZ_CF_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN")
        )
    )
    # base URL の上書き（既定は api.cloudflare.com）。`/client/v4/accounts/{id}/ai/run` と
    # 同じ形を話すプロキシを挟むときに使う。AI Gateway は URL の形が違うため非対応。
    cf_base_url: str | None = field(default_factory=lambda: os.environ.get("NIZ_CF_BASE_URL"))
    concurrency: int = field(
        default_factory=lambda: int(os.environ.get("NIZ_CONCURRENCY", DEFAULT_CONCURRENCY))
    )
    log_subjects: bool = field(
        default_factory=lambda: os.environ.get("NIZ_LOG_SUBJECTS", "1") != "0"
    )

    def __post_init__(self) -> None:
        # トークンはリフレッシュ時に書き戻しが必要なためファイルで持つが、
        # リポジトリ内には置かない。NIZ_TOKEN_DIR で場所を変えられる。
        if self.token_path is None:
            token_dir = Path(
                os.environ.get("NIZ_TOKEN_DIR", _state_home() / "numa-inbox-zero" / "tokens")
            )
            self.token_path = token_dir / f"{self.account}.json"
        # 作業ファイルはアカウントごとに分離し、別アカウントの実行で
        # inbox.json / classification.json が上書きされる事故を防ぐ。
        if self.work_dir is None:
            self.work_dir = PROJECT_ROOT / "work" / self.account

    @property
    def inbox_path(self) -> Path:
        return self.work_dir / "inbox.json"

    @property
    def classification_path(self) -> Path:
        return self.work_dir / "classification.json"

    @property
    def classify_meta_path(self) -> Path:
        return self.work_dir / "classify_meta.json"

    @property
    def eval_dir(self) -> Path:
        return self.root / "eval"

    @property
    def golden_path(self) -> Path:
        return self.eval_dir / "golden.jsonl"

    @property
    def eval_results_dir(self) -> Path:
        return self.eval_dir / "results"

    @property
    def runs_log_path(self) -> Path:
        return self.logs_dir / "runs.jsonl"

    @property
    def decisions_log_path(self) -> Path:
        return self.logs_dir / "decisions.jsonl"

    def ensure_dirs(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
