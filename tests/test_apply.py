"""apply.py（適用ロジック）のテスト。Gmail API はスタブに差し替える。"""

from unittest.mock import patch

import httplib2
from googleapiclient.errors import HttpError

from numa_inbox_zero.apply import apply_decisions
from numa_inbox_zero.validate import validate_decisions


def _decision(message_id="m1", action="archive"):
    return {"message_id": message_id, "action": action, "category": "c", "reason": "r"}


def _http_error() -> HttpError:
    return HttpError(httplib2.Response({"status": 500}), b"boom")


def _run(decisions, allowed_ids, dry_run=False, modify=None):
    validation = validate_decisions(decisions, set(allowed_ids))
    with patch("numa_inbox_zero.apply.gmail.modify_labels", side_effect=modify) as mock_modify:
        summary = apply_decisions(
            service=object(),
            validation=validation,
            processed_label_id="Label_1",
            dry_run=dry_run,
        )
    return summary, mock_modify


class TestApplyActions:
    def test_archiveはINBOXを外しprocessedを付ける(self):
        summary, mock_modify = _run([_decision()], {"m1"})
        assert summary.archived == 1
        mock_modify.assert_called_once()
        _, kwargs = mock_modify.call_args
        assert kwargs["add"] == ["Label_1"]
        assert kwargs["remove"] == ["INBOX"]

    def test_starはSTARREDとprocessedを付けINBOXから外す(self):
        summary, mock_modify = _run([_decision(action="star")], {"m1"})
        assert summary.starred == 1
        _, kwargs = mock_modify.call_args
        assert kwargs["add"] == ["STARRED", "Label_1"]
        assert kwargs["remove"] == ["INBOX"]

    def test_UNREADはどのアクションでも外さない(self):
        for action in ("archive", "star"):
            _, mock_modify = _run([_decision(action=action)], {"m1"})
            _, kwargs = mock_modify.call_args
            assert "UNREAD" not in kwargs["remove"]

    def test_ドラフト作成APIは存在しない(self):
        """送信・下書き系の API をコードベースに戻さないための番人。"""
        from numa_inbox_zero import gmail

        assert not hasattr(gmail, "create_reply_draft")
        assert not [name for name in dir(gmail) if "draft" in name.lower()]


class TestFailureHandling:
    def test_ラベル操作の失敗はfailedとして記録される(self):
        summary, _ = _run([_decision()], {"m1"}, modify=_http_error())
        assert summary.errors == 1
        assert summary.archived == 0
        record = summary.records[0]
        assert record.applied == "failed"
        assert "ラベル操作に失敗" in record.failure_reason

    def test_拒否された判定はrejectedとして記録され件数に入る(self):
        summary, mock_modify = _run([_decision(message_id="unknown")], {"m1"})
        assert summary.rejected == 1
        assert summary.archived == 0
        mock_modify.assert_not_called()


class TestDryRun:
    def test_dry_runではAPIを一切呼ばない(self):
        summary, mock_modify = _run(
            [_decision("m1", "archive"), _decision("m2", "star")],
            {"m1", "m2"},
            dry_run=True,
        )
        mock_modify.assert_not_called()
        assert summary.archived == 1
        assert summary.starred == 1
        assert all(r.applied == "dry-run" for r in summary.records)
