"""Gmail API ラッパ。

ネットワークを触る処理はすべてここに集約する。純粋な変換処理は
mail.py にあり、このモジュールは API 呼び出しの組み立てに徹する。
"""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import GMAIL_SCOPES, PROCESSED_LABEL, Config
from .mail import extract_body, get_header, truncate_body


def load_client_config(credentials_json: str | None, credentials_path: Path) -> dict:
    """OAuth クライアント設定を読み込む。

    1Password の env mount（NIZ_CREDENTIALS_JSON に JSON の中身が注入される）を
    優先し、無ければファイルパスにフォールバックする。クライアント設定は
    アプリの身元なので全アカウント共通で 1 つでよい。
    """
    if credentials_json:
        try:
            config = json.loads(credentials_json)
        except json.JSONDecodeError as e:
            raise ValueError(
                "NIZ_CREDENTIALS_JSON の中身が JSON としてパースできない。"
                "1Password のフィールドにクライアント設定 JSON 全体が入っているか確認してください。"
            ) from e
        if not isinstance(config, dict) or not ({"installed", "web"} & set(config)):
            raise ValueError(
                "NIZ_CREDENTIALS_JSON が OAuth クライアント設定の形式ではない"
                "（トップレベルに 'installed' か 'web' が必要）。"
            )
        return config

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"OAuth クライアント設定が見つからない: {credentials_path}\n"
            "1Password 経由なら NIZ_CREDENTIALS_JSON を注入（op run --env-file=.env.op -- ...）、"
            "ファイルなら credentials.json を配置するか NIZ_CREDENTIALS で場所を指定してください。"
        )
    return json.loads(credentials_path.read_text(encoding="utf-8"))


def get_credentials(cfg: Config) -> Credentials:
    """保存済みトークンを読み、期限切れならリフレッシュ、無ければ OAuth フローを起動する。

    トークンはアカウントごとに別ファイル（cfg.token_path）。初回の OAuth フローは
    ブラウザを開くため対話環境でしか動かない。定期実行の前にアカウントごとに
    `numa-inbox-zero auth --account <name>` を手動で実行しておくこと。
    """
    creds: Credentials | None = None
    if cfg.token_path.exists():
        creds = Credentials.from_authorized_user_file(str(cfg.token_path), GMAIL_SCOPES)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # リフレッシュトークン自体が失効・取り消し済み（invalid_grant）。
            # OAuth 同意画面が「テスト」ステータスだと 7 日で失効する。
            # 保存済みトークンは使いものにならないので破棄してフローをやり直す。
            creds = None
    if creds is None or not creds.valid:
        client_config = load_client_config(cfg.credentials_json, cfg.credentials_path)
        flow = InstalledAppFlow.from_client_config(client_config, GMAIL_SCOPES)
        # WSL ではブラウザの自動起動が壊れた WSL 内ブラウザを掴み、リダイレクトが
        # ローカルサーバに届かずタイムアウトする。自動起動はせず URL を表示し、
        # Windows 側のブラウザで開いてもらう（WSL2 は Windows → localhost の
        # ポート転送が効くため、リダイレクトはそのまま届く）。
        creds = flow.run_local_server(
            port=0,
            open_browser=False,
            authorization_prompt_message=(
                "次の URL をブラウザで開いて認証してください"
                "（WSL の場合は Windows 側のブラウザで開く）:\n{url}"
            ),
        )

    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text(creds.to_json(), encoding="utf-8")
    # リフレッシュトークンは Gmail への実質的な鍵なので所有者のみ読める状態にする
    cfg.token_path.chmod(0o600)
    return creds


def get_service(cfg: Config):
    creds = get_credentials(cfg)
    # cache_discovery=False: ローカルキャッシュ書き込みの警告抑止
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def ensure_processed_label(service) -> str:
    """処理済みラベルの ID を返す。存在しなければ作成する。"""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label.get("name") == PROCESSED_LABEL:
            return label["id"]
    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": PROCESSED_LABEL,
                "labelListVisibility": "labelHide",
                "messageListVisibility": "hide",
            },
        )
        .execute()
    )
    return created["id"]


def fetch_inbox_messages(service, query: str, max_results: int, body_limit: int) -> list[dict]:
    """クエリに一致する受信トレイのメールを取得し、分類器に渡す形へ整形する。"""
    listing = (
        service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    )
    refs = listing.get("messages", [])

    messages = []
    for ref in refs:
        raw = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        messages.append(_shape_message(raw, body_limit))
    return messages


def fetch_messages_by_ids(
    service, message_ids: list[str], body_limit: int
) -> tuple[list[dict], list[str]]:
    """message_id 指定でメールを取得し、分類器に渡す形へ整形する。

    アーカイブ済み・受信トレイ外のメールも取得できる。eval import が
    実行ログから過去のメールを復元するために使う（読み取りのみ）。
    削除済み等で見つからない ID は missing として返し、呼び出し側が報告する。
    """
    messages = []
    missing = []
    for message_id in message_ids:
        try:
            raw = (
                service.users().messages().get(userId="me", id=message_id, format="full").execute()
            )
        except HttpError as e:
            if e.resp.status == 404:
                missing.append(message_id)
                continue
            raise
        messages.append(_shape_message(raw, body_limit))
    return messages, missing


def _shape_message(raw: dict, body_limit: int) -> dict:
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])
    body, truncated = truncate_body(extract_body(payload), body_limit)
    return {
        "message_id": raw["id"],
        "thread_id": raw.get("threadId", raw["id"]),
        "from": get_header(headers, "From"),
        "to": get_header(headers, "To"),
        "subject": get_header(headers, "Subject"),
        "date": get_header(headers, "Date"),
        "labels": raw.get("labelIds", []),
        "body": body,
        "body_truncated": truncated,
    }


def modify_labels(service, message_id: str, add: list[str], remove: list[str]) -> None:
    body: dict = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    service.users().messages().modify(userId="me", id=message_id, body=body).execute()
