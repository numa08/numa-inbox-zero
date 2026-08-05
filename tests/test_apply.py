"""apply.py（適用ロジック）のテスト。Gmail API はスタブに差し替える。"""

from unittest.mock import patch

from numa_inbox_zero.apply import apply_decisions
from numa_inbox_zero.validate import validate_decisions


def _message(message_id="m1"):
    return {
        "message_id": message_id,
        "thread_id": f"t-{message_id}",
        "from": "taro@example.com",
        "subject": "件名",
        "reply_to": "",
        "rfc_message_id": f"<{message_id}@example.com>",
        "rfc_references": "",
        "body": "本文",
    }


def _run(decisions, messages, dry_run=False, modify=None, draft=None):
    validation = validate_decisions(decisions, {m["message_id"] for m in messages})
    with (
        patch("numa_inbox_zero.apply.gmail.modify_labels", side_effect=modify) as mock_modify,
        patch(
            "numa_inbox_zero.apply.gmail.create_reply_draft",
            side_effect=draft,
            return_value="draft-1" if draft is None else None,
        ) as mock_draft,
    ):
        summary = apply_decisions(
            service=object(),
            validation=validation,
            messages_by_id={m["message_id"]: m for m in messages},
            processed_label_id="Label_1",
            dry_run=dry_run,
        )
    return summary, mock_modify, mock_draft


class TestApplyActions:
    def test_archiveはINBOXを外しprocessedを付ける(self):
        summary, mock_modify, _ = _run(
            [{"message_id": "m1", "action": "archive", "reason": "r"}], [_message()]
        )
        assert summary.archived == 1
        mock_modify.assert_called_once()
        _, kwargs = mock_modify.call_args
        assert kwargs["add"] == ["Label_1"]
        assert kwargs["remove"] == ["INBOX"]

    def test_starはSTARREDとprocessedを付けINBOXから外す(self):
        summary, mock_modify, _ = _run(
            [{"message_id": "m1", "action": "star", "reason": "r"}], [_message()]
        )
        assert summary.starred == 1
        _, kwargs = mock_modify.call_args
        assert kwargs["add"] == ["STARRED", "Label_1"]
        assert kwargs["remove"] == ["INBOX"]

    def test_replyのラベル付与でもINBOXから外す(self):
        summary, mock_modify, _ = _run(
            [{"message_id": "m1", "action": "reply", "reason": "r", "draft_body": "承知"}],
            [_message()],
        )
        assert summary.drafted == 1
        _, kwargs = mock_modify.call_args
        assert kwargs["add"] == ["STARRED", "Label_1"]
        assert kwargs["remove"] == ["INBOX"]

    def test_replyはラベル付与のあとにドラフト作成(self):
        call_order = []
        summary, _, _ = _run(
            [{"message_id": "m1", "action": "reply", "reason": "r", "draft_body": "承知"}],
            [_message()],
            modify=lambda *a, **k: call_order.append("modify"),
            draft=lambda *a, **k: call_order.append("draft") or "draft-1",
        )
        assert summary.drafted == 1
        assert call_order == ["modify", "draft"]

    def test_UNREADはどのアクションでも外さない(self):
        for action in ("archive", "star"):
            _, mock_modify, _ = _run(
                [{"message_id": "m1", "action": action, "reason": "r"}], [_message()]
            )
            _, kwargs = mock_modify.call_args
            assert "UNREAD" not in kwargs["remove"]


class TestFailureHandling:
    def test_ドラフト作成失敗はpartialとして記録される(self):
        summary, _, _ = _run(
            [{"message_id": "m1", "action": "reply", "reason": "r", "draft_body": "b"}],
            [_message()],
            draft=ValueError("返信先が特定できない"),
        )
        assert summary.errors == 1
        record = summary.records[0]
        assert record.applied == "partial"
        assert "ドラフト作成に失敗" in record.partial_reason

    def test_拒否された判定はrejectedとして記録され件数に入る(self):
        summary, mock_modify, _ = _run(
            [{"message_id": "unknown", "action": "archive", "reason": "r"}],
            [_message()],
        )
        assert summary.rejected == 1
        assert summary.archived == 0
        mock_modify.assert_not_called()


class TestDryRun:
    def test_dry_runではAPIを一切呼ばない(self):
        summary, mock_modify, mock_draft = _run(
            [
                {"message_id": "m1", "action": "archive", "reason": "r"},
                {"message_id": "m2", "action": "reply", "reason": "r", "draft_body": "b"},
            ],
            [_message("m1"), _message("m2")],
            dry_run=True,
        )
        mock_modify.assert_not_called()
        mock_draft.assert_not_called()
        assert summary.archived == 1
        assert summary.drafted == 1
        assert all(r.applied == "dry-run" for r in summary.records)
