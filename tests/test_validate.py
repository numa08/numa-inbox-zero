"""validate.py（分類結果の検証と archive ゲート = インジェクション対策の砦）のテスト。"""

from numa_inbox_zero.policy import Thresholds
from numa_inbox_zero.validate import gate_archive, validate_decisions

THRESHOLDS = Thresholds(archive_confidence=0.85, injection=0.5)


def _decision(message_id="m1", action="archive", **kw):
    base = {
        "message_id": message_id,
        "action": action,
        "category": "newsletter_promotion",
        "confidence": 0.95,
        "injection": 0.01,
        "reason": "r",
    }
    return {**base, **kw}


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

    def test_actionがreplyでも拒否される(self):
        """返信ドラフト機能は廃止済み。復活の経路を塞ぐ。"""
        result = validate_decisions([_decision(action="reply")], {"m1"})
        assert result.accepted == []

    def test_policyに無いカテゴリはactionがNoneになり拒否される(self):
        """モデルの応答と policy.toml がずれた場合に何も実行されない。"""
        result = validate_decisions([_decision(action=None, category="unknown_category")], {"m1"})
        assert result.accepted == []
        assert "unknown_category" in result.rejected[0].reason

    def test_重複message_idは2件目以降を拒否(self):
        decisions = [_decision(action="archive"), _decision(action="star")]
        result = validate_decisions(decisions, {"m1"})
        assert len(result.accepted) == 1
        assert result.accepted[0]["action"] == "archive"
        assert len(result.rejected) == 1

    def test_message_idが数値でも文字列比較で判定する(self):
        result = validate_decisions([_decision(message_id=123)], {"123"})
        assert len(result.accepted) == 1

    def test_空のdecisionsは空の結果(self):
        result = validate_decisions([], {"m1"})
        assert result.accepted == []
        assert result.rejected == []


class TestGateArchive:
    def test_閾値未満のarchiveはstarに降格され元の判定が残る(self):
        result, count = gate_archive([_decision(confidence=0.7)], THRESHOLDS)
        assert count == 1
        assert result[0]["action"] == "star"
        assert result[0]["downgraded_from"] == "archive"
        assert "confidence" in result[0]["downgrade_reason"]

    def test_閾値以上のarchiveはそのまま(self):
        result, count = gate_archive([_decision(confidence=0.85)], THRESHOLDS)
        assert count == 0
        assert result[0]["action"] == "archive"

    def test_confidence欠損のarchiveは降格される(self):
        decision = _decision()
        del decision["confidence"]
        result, count = gate_archive([decision], THRESHOLDS)
        assert count == 1
        assert result[0]["action"] == "star"

    def test_不正なconfidence値のarchiveも降格される(self):
        for bad in ("high", None, 1.5, -0.1, True):
            result, count = gate_archive([_decision(confidence=bad)], THRESHOLDS)
            assert count == 1, bad
            assert result[0]["action"] == "star"

    def test_インジェクションが疑われるarchiveは降格される(self):
        """本文が処理系に指示を出そうとしているメールは不可逆側に倒さない。"""
        result, count = gate_archive([_decision(injection=0.9)], THRESHOLDS)
        assert count == 1
        assert result[0]["action"] == "star"
        assert "injection" in result[0]["downgrade_reason"]

    def test_injection欠損のarchiveも降格される(self):
        decision = _decision()
        del decision["injection"]
        result, count = gate_archive([decision], THRESHOLDS)
        assert count == 1

    def test_starは確信度に関わらず降格されない(self):
        result, count = gate_archive(
            [_decision(action="star", confidence=0.1, injection=0.99)], THRESHOLDS
        )
        assert count == 0
        assert result[0]["action"] == "star"

    def test_入力のdictを変更しない(self):
        original = _decision(confidence=0.1)
        gate_archive([original], THRESHOLDS)
        assert original["action"] == "archive"
        assert "downgraded_from" not in original
