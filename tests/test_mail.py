"""mail.py（純粋な変換処理）のテスト。"""

import base64
from email import message_from_bytes, policy

from numa_inbox_zero.mail import (
    build_reply_mime,
    build_reply_references,
    build_reply_subject,
    choose_reply_address,
    decode_body_data,
    extract_body,
    from_domain,
    get_header,
    html_to_text,
    truncate_body,
)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class TestDecodeBodyData:
    def test_パディング欠落を補って復号できる(self):
        assert decode_body_data(_b64url("こんにちは")) == "こんにちは"

    def test_不正なバイト列でも例外を出さない(self):
        data = base64.urlsafe_b64encode(b"\xff\xfe\x00abc").decode("ascii")
        result = decode_body_data(data)
        assert "abc" in result


class TestHtmlToText:
    def test_タグを除去してテキストを残す(self):
        assert html_to_text("<p>Hello <b>World</b></p>") == "Hello World"

    def test_scriptとstyleは中身ごと消える(self):
        html = "<style>body{color:red}</style><p>本文</p><script>alert(1)</script>"
        assert html_to_text(html) == "本文"

    def test_brとpは改行になる(self):
        assert html_to_text("<p>一行目</p><p>二行目<br>三行目</p>") == "一行目\n二行目\n三行目"

    def test_エンティティを展開する(self):
        assert html_to_text("A &amp; B &lt;C&gt;") == "A & B <C>"


class TestExtractBody:
    def test_単一パートのtext_plain(self):
        payload = {"mimeType": "text/plain", "body": {"data": _b64url("本文です")}}
        assert extract_body(payload) == "本文です"

    def test_multipartではtext_plainを優先する(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64url("<p>HTML</p>")}},
                {"mimeType": "text/plain", "body": {"data": _b64url("PLAIN")}},
            ],
        }
        assert extract_body(payload) == "PLAIN"

    def test_text_plainがなければHTMLをタグ除去して使う(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64url("<p>HTML本文</p>")}},
            ],
        }
        assert extract_body(payload) == "HTML本文"

    def test_ネストしたmultipartを探索する(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64url("深い本文")}},
                    ],
                },
            ],
        }
        assert extract_body(payload) == "深い本文"

    def test_本文がなければ空文字(self):
        assert extract_body({"mimeType": "multipart/mixed", "parts": []}) == ""


class TestTruncateBody:
    def test_上限以下ならそのまま(self):
        assert truncate_body("abc", 10) == ("abc", False)

    def test_上限を超えたら切り詰めてフラグを立てる(self):
        body, truncated = truncate_body("あ" * 100, 10)
        assert body == "あ" * 10
        assert truncated is True


class TestGetHeader:
    def test_大文字小文字を無視して取得(self):
        headers = [{"name": "Message-ID", "value": "<abc@example.com>"}]
        assert get_header(headers, "message-id") == "<abc@example.com>"

    def test_存在しなければ空文字(self):
        assert get_header([], "Subject") == ""


class TestBuildReplySubject:
    def test_Reを付与する(self):
        assert build_reply_subject("見積の件") == "Re: 見積の件"

    def test_既にReで始まっていれば重複させない(self):
        assert build_reply_subject("Re: 見積の件") == "Re: 見積の件"

    def test_大文字REも重複させない(self):
        assert build_reply_subject("RE: Quote") == "RE: Quote"

    def test_re_の前後に空白があっても検知する(self):
        assert build_reply_subject(" re : hello") == " re : hello"


class TestChooseReplyAddress:
    def test_ReplyToを優先する(self):
        assert choose_reply_address("reply@example.com", "from@example.com") == "reply@example.com"

    def test_ReplyToがなければFrom(self):
        assert choose_reply_address("", "Taro <from@example.com>") == "Taro <from@example.com>"

    def test_どちらも無効なら空文字(self):
        assert choose_reply_address("", "") == ""


class TestBuildReplyReferences:
    def test_ReferencesにMessageIDを連結(self):
        assert build_reply_references("<a@x> <b@x>", "<c@x>") == "<a@x> <b@x> <c@x>"

    def test_Referencesが空ならMessageIDのみ(self):
        assert build_reply_references("", "<c@x>") == "<c@x>"

    def test_両方空なら空文字(self):
        assert build_reply_references("", "") == ""


class TestBuildReplyMime:
    def _decode(self, raw: str):
        padded = raw + "=" * (-len(raw) % 4)
        # policy.default で RFC 2047 エンコードされたヘッダを自動復号する
        return message_from_bytes(base64.urlsafe_b64decode(padded), policy=policy.default)

    def test_スレッド紐付けヘッダが入る(self):
        raw = build_reply_mime(
            to_addr="taro@example.com",
            subject="Re: 見積の件",
            body="承知しました。",
            in_reply_to="<orig@example.com>",
            references="<root@example.com> <orig@example.com>",
        )
        msg = self._decode(raw)
        assert msg["To"] == "taro@example.com"
        assert msg["In-Reply-To"] == "<orig@example.com>"
        assert msg["References"] == "<root@example.com> <orig@example.com>"

    def test_日本語の件名と本文が復元できる(self):
        raw = build_reply_mime(
            to_addr="taro@example.com",
            subject="Re: 見積の件",
            body="お世話になっております。",
            in_reply_to="<orig@example.com>",
            references="<orig@example.com>",
        )
        msg = self._decode(raw)
        assert "見積の件" in str(msg["Subject"])
        assert "お世話になっております。" in msg.get_content()

    def test_ヘッダが空ならInReplyToとReferencesを付けない(self):
        raw = build_reply_mime(
            to_addr="taro@example.com",
            subject="Re: x",
            body="b",
            in_reply_to="",
            references="",
        )
        msg = self._decode(raw)
        assert msg["In-Reply-To"] is None
        assert msg["References"] is None


class TestFromDomain:
    def test_表示名付きアドレスからドメインを取る(self):
        assert from_domain("Taro Yamada <taro@Example.COM>") == "example.com"

    def test_アドレスがなければ空文字(self):
        assert from_domain("not-an-address") == ""
