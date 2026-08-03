"""validate.py（分類結果の検証 = インジェクション対策の砦）のテスト。"""

from numa_inbox_zero.validate import downgrade_low_confidence, validate_decisions


def _decision(message_id="m1", action="archive", **kw):
    return {"message_id": message_id, "action": action, "reason": "r", **kw}


class TestValidateDecisions:
    def test_正常な判定は受理される(self):
        result = validate_decisions([_decision()], {"m1"})
        assert len(result.accepted) == 1
        assert result.rejected == []

    def test_入力に存在しないmessage_idは拒否(self):
        """本文中の指示で捏造された ID が実行されないことを保証する。"""
        result = validate_decisions([_decision(message_id="evil")], {"m1"})
        assert result.accepted == []
        assert result.rejected[0].message_id == "evil"

    def test_enum外のactionは拒否(self):
        result = validate_decisions([_decision(action="delete")], {"m1"})
        assert result.accepted == []
        assert "delete" in result.rejected[0].reason

    def test_actionがsendでも拒否される(self):
        """送信系のアクションが紛れ込んでも実行されない。"""
        result = validate_decisions([_decision(action="send")], {"m1"})
        assert result.accepted == []

    def test_重複message_idは2件目以降を拒否(self):
        decisions = [_decision(action="archive"), _decision(action="star")]
        result = validate_decisions(decisions, {"m1"})
        assert len(result.accepted) == 1
        assert result.accepted[0]["action"] == "archive"
        assert len(result.rejected) == 1

    def test_replyでdraft_bodyが空なら拒否(self):
        result = validate_decisions([_decision(action="reply", draft_body="")], {"m1"})
        assert result.accepted == []

    def test_replyでdraft_bodyが空白のみでも拒否(self):
        result = validate_decisions([_decision(action="reply", draft_body="  \n ")], {"m1"})
        assert result.accepted == []

    def test_replyでdraft_bodyがあれば受理(self):
        result = validate_decisions(
            [_decision(action="reply", draft_body="承知しました。")], {"m1"}
        )
        assert len(result.accepted) == 1

    def test_message_idが数値でも文字列比較で判定する(self):
        result = validate_decisions(
            [{"message_id": 123, "action": "archive", "reason": "r"}], {"123"}
        )
        assert len(result.accepted) == 1

    def test_空のdecisionsは空の結果(self):
        result = validate_decisions([], {"m1"})
        assert result.accepted == []
        assert result.rejected == []


class TestDowngradeLowConfidence:
    def test_lowのarchiveはstarに降格され元の判定が残る(self):
        decisions = [_decision(action="archive", confidence="low")]
        result, count = downgrade_low_confidence(decisions)
        assert count == 1
        assert result[0]["action"] == "star"
        assert result[0]["downgraded_from"] == "archive"

    def test_highのarchiveはそのまま(self):
        decisions = [_decision(action="archive", confidence="high")]
        result, count = downgrade_low_confidence(decisions)
        assert count == 0
        assert result[0]["action"] == "archive"

    def test_confidence欠損のarchiveは降格される(self):
        """スキーマ拘束が効かなかった場合も安全側（star）に倒す。"""
        result, count = downgrade_low_confidence([_decision(action="archive")])
        assert count == 1
        assert result[0]["action"] == "star"

    def test_不正なconfidence値のarchiveも降格される(self):
        result, count = downgrade_low_confidence(
            [_decision(action="archive", confidence="very-sure")]
        )
        assert count == 1

    def test_starとreplyは確信度に関わらず降格されない(self):
        decisions = [
            _decision(message_id="m1", action="star", confidence="low"),
            _decision(message_id="m2", action="reply", confidence="low", draft_body="b"),
        ]
        result, count = downgrade_low_confidence(decisions)
        assert count == 0
        assert [d["action"] for d in result] == ["star", "reply"]

    def test_入力のdictを変更しない(self):
        original = _decision(action="archive", confidence="low")
        downgrade_low_confidence([original])
        assert original["action"] == "archive"
        assert "downgraded_from" not in original
