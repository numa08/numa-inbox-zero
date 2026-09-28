"""実行時設定。

複数アカウント対応の考え方:
- OAuth クライアント（キー/シークレット）はアプリの身元なので全アカウント共通で 1 つ
- アカウントごとに分かれるのはトークン・作業ディレクトリ
- ログは全アカウント共有の JSONL に account フィールド付きで追記し、横断集計できるようにする

認証情報はリポジトリ内に置かない:
- クライアント設定は 1Password の env mount（NIZ_CREDENTIALS_JSON に JSON の中身）で注入
- トークンは XDG state（~/.local/state/numa-inbox-zero/）に保存
- TypeSafe の API キーも env mount（NIZ_TYPESAFE_API_KEY）で注入する
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

# Jev の既定モデル。confidence の閾値はモデルバージョンごとに較正されるため、
# alias（jev-latest）ではなくバージョンを固定する。上げるときは policy.toml の
# 閾値を eval で再確認する。
DEFAULT_MODEL = "jev-1.13.0"

# 分類の同時リクエスト数。レート上限は 1200 req/min なので余裕があるが、
# 50 通を 8 並列で回せば数秒で終わるため上限を狙う必要はない。
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

    # Jev のモデル ID。閾値は較正済み confidence に紐づくので alias ではなく
    # バージョンを固定する（policy.toml の閾値とセットで意味を持つ）。
    model: str = field(default_factory=lambda: os.environ.get("NIZ_MODEL", DEFAULT_MODEL))
    # SDK 標準の TYPESAFE_API_KEY も受ける（TypeSafe のドキュメントどおりの環境でも動くように）。
    typesafe_api_key: str | None = field(
        default_factory=lambda: (
            os.environ.get("NIZ_TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
        )
    )
    # OpenRouter 等のゲートウェイ経由にする場合のみ設定する。未設定なら API 直叩き。
    typesafe_base_url: str | None = field(
        default_factory=lambda: os.environ.get("NIZ_TYPESAFE_BASE_URL")
    )
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
