"""mail.py（純粋な変換処理）のテスト。"""

import base64

from numa_inbox_zero.mail import (
    decode_body_data,
    extract_body,
    from_domain,
    get_header,
    html_to_text,
    sender_name,
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

    def test_multipartではhtmlをタグ除去して優先する(self):
        """text/plain が「HTML メールをご覧いただけません」等の代替文のみで
        実内容を含まないメールがあるため、実内容が入っている HTML 側を正とする。"""
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64url("<p>HTML本文</p>")}},
                {"mimeType": "text/plain", "body": {"data": _b64url("代替文のみ")}},
            ],
        }
        assert extract_body(payload) == "HTML本文"

    def test_htmlがなければtext_plainを使う(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("PLAIN")}},
            ],
        }
        assert extract_body(payload) == "PLAIN"

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


class TestFromDomain:
    def test_表示名付きアドレスからドメインを取る(self):
        assert from_domain("Taro Yamada <taro@Example.COM>") == "example.com"

    def test_アドレスがなければ空文字(self):
        assert from_domain("not-an-address") == ""


class TestSenderName:
    def test_表示名を取り出す(self):
        assert sender_name("Taro Yamada <taro@example.com>") == "Taro Yamada"

    def test_表示名がなければ空文字(self):
        assert sender_name("taro@example.com") == ""
