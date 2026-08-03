"""メール本文・ヘッダの純粋な変換処理。

Gmail API に触らない関数だけを置く。ここに置いたものはすべて
ネットワークなしでユニットテストできる。
"""

from __future__ import annotations

import base64
import html
import re
from email.message import EmailMessage
from email.utils import parseaddr


def decode_body_data(data: str) -> str:
    """Gmail API の body.data（base64url）をテキストに復元する。"""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


_TAG_DROP_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RE = re.compile(r"\n{3,}")


def html_to_text(html_body: str) -> str:
    """HTML メールから読めるテキストを取り出す。

    分類器に渡せる程度の品質でよいので、正規表現ベースの簡易実装にとどめる。
    """
    text = _TAG_DROP_RE.sub("", html_body)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RE.sub("\n\n", text)
    return text.strip()


def extract_body(payload: dict) -> str:
    """Gmail の payload ツリーから本文テキストを取り出す。

    text/plain を優先し、なければ text/html をタグ除去して使う。
    multipart はネストするため再帰的に探索する。
    """
    plain = _find_part(payload, "text/plain")
    if plain is not None:
        return plain
    html_part = _find_part(payload, "text/html")
    if html_part is not None:
        return html_to_text(html_part)
    return ""


def _find_part(payload: dict, mime_type: str) -> str | None:
    if payload.get("mimeType") == mime_type:
        data = payload.get("body", {}).get("data")
        if data:
            return decode_body_data(data)
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime_type)
        if found is not None:
            return found
    return None


def truncate_body(body: str, limit: int) -> tuple[str, bool]:
    """本文を limit 文字に切り詰める。戻り値は (本文, 切り詰めたか)。"""
    if len(body) <= limit:
        return body, False
    return body[:limit], True


def get_header(headers: list[dict], name: str) -> str:
    """Gmail API のヘッダ配列から値を取り出す（大文字小文字を無視）。"""
    lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == lower:
            return h.get("value", "")
    return ""


_RE_PREFIX = re.compile(r"^\s*re\s*:\s*", re.IGNORECASE)


def build_reply_subject(original_subject: str) -> str:
    """返信件名を作る。既に Re: で始まっていれば重複させない。"""
    if _RE_PREFIX.match(original_subject):
        return original_subject
    return f"Re: {original_subject}"


def choose_reply_address(reply_to: str, from_addr: str) -> str:
    """返信先を決める。Reply-To があればそれを優先する。"""
    for candidate in (reply_to, from_addr):
        _, addr = parseaddr(candidate)
        if addr:
            return candidate.strip()
    return ""


def build_reply_references(original_references: str, original_message_id: str) -> str:
    """RFC 5322 の References ヘッダを組み立てる。

    元スレッドの References に元メールの Message-ID を連結する。
    どちらも無ければ空文字（ヘッダ自体を付けない）。
    """
    parts = [p for p in (original_references.strip(), original_message_id.strip()) if p]
    return " ".join(parts)


def build_reply_mime(
    *,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str,
    references: str,
) -> str:
    """返信ドラフトの MIME を組み立て、Gmail API 用の base64url 文字列を返す。

    In-Reply-To / References を付けないと Gmail 上でスレッドに紐づかず
    孤立した下書きになるため、元メールのヘッダから必ず引き継ぐ。
    """
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body, charset="utf-8")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def from_domain(from_header: str) -> str:
    """From ヘッダからドメインだけを取り出す。

    ログにはアドレス全体を残さない（送信者別の傾向分析にはドメインで足りる）。
    """
    _, addr = parseaddr(from_header)
    if "@" in addr:
        return addr.rsplit("@", 1)[1].lower()
    return ""
