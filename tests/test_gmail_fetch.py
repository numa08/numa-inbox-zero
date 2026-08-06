"""gmail.py の取得系のテスト（Gmail API はモック、ネットワークなし）。"""

from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from numa_inbox_zero.gmail import fetch_messages_by_ids


def _http_error(status):
    return HttpError(httplib2.Response({"status": str(status)}), b"")


def _service(responses):
    """message_id → 生レスポンス（または例外）の対応で messages.get を偽装する。"""

    def get(userId, id, format):
        request = MagicMock()
        value = responses[id]
        if isinstance(value, Exception):
            request.execute.side_effect = value
        else:
            request.execute.return_value = value
        return request

    service = MagicMock()
    service.users.return_value.messages.return_value.get.side_effect = get
    return service


class TestFetchMessagesByIds:
    def test_id指定で取得し分類器に渡す形へ整形する(self):
        service = _service({"m1": {"id": "m1", "threadId": "t1", "payload": {"headers": []}}})
        messages, missing = fetch_messages_by_ids(service, ["m1"], body_limit=100)
        assert missing == []
        assert messages[0]["message_id"] == "m1"
        assert messages[0]["thread_id"] == "t1"
        assert "body" in messages[0]

    def test_404のメールはmissingとして返し他は継続する(self):
        """削除済みメールで全体を落とさない。eval import が件数を報告できるようにする。"""
        service = _service(
            {
                "m1": _http_error(404),
                "m2": {"id": "m2", "payload": {"headers": []}},
            }
        )
        messages, missing = fetch_messages_by_ids(service, ["m1", "m2"], body_limit=100)
        assert missing == ["m1"]
        assert [m["message_id"] for m in messages] == ["m2"]

    def test_404以外のエラーは伝播する(self):
        """権限エラーや 429 を黙って欠損扱いにすると気づけないため。"""
        service = _service({"m1": _http_error(403)})
        with pytest.raises(HttpError):
            fetch_messages_by_ids(service, ["m1"], body_limit=100)
