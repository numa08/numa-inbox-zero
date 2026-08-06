"""runlog.py（JSONL ログ）のテスト。"""

import json

import pytest

from numa_inbox_zero.apply import ApplyRecord
from numa_inbox_zero.runlog import append_jsonl, build_decision_record, load_jsonl


class TestLoadJsonl:
    def test_追記した内容がそのまま読み戻せる(self, tmp_path):
        path = tmp_path / "d.jsonl"
        append_jsonl(path, {"a": 1})
        append_jsonl(path, {"b": "日本語"})
        assert load_jsonl(path) == [{"a": 1}, {"b": "日本語"}]

    def test_存在しないファイルは空リスト(self, tmp_path):
        assert load_jsonl(tmp_path / "none.jsonl") == []

    def test_壊れた行は行番号付きでエラー(self, tmp_path):
        path = tmp_path / "d.jsonl"
        path.write_text('{"ok": 1}\n{broken\n', encoding="utf-8")
        with pytest.raises(ValueError, match=":2"):
            load_jsonl(path)


class TestAppendJsonl:
    def test_1行ずつ追記される(self, tmp_path):
        path = tmp_path / "logs" / "runs.jsonl"
        append_jsonl(path, {"a": 1})
        append_jsonl(path, {"b": "日本語"})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0]) == {"a": 1}
        assert json.loads(lines[1]) == {"b": "日本語"}

    def test_日本語がエスケープされずそのまま残る(self, tmp_path):
        path = tmp_path / "d.jsonl"
        append_jsonl(path, {"subject": "見積の件"})
        assert "見積の件" in path.read_text(encoding="utf-8")


class TestBuildDecisionRecord:
    def _message(self):
        return {
            "thread_id": "t1",
            "from": "Taro <taro@example.com>",
            "subject": "見積の件",
            "body": "本文" * 10,
        }

    def test_メールアドレス全体は記録せずドメインのみ(self):
        record = build_decision_record(
            run_id="r1",
            account="personal",
            record=ApplyRecord("m1", "star", "ok"),
            decision={"reason": "要確認"},
            message=self._message(),
            log_subjects=True,
        )
        assert record["from_domain"] == "example.com"
        assert "taro@" not in json.dumps(record)

    def test_log_subjects_falseなら件名を記録しない(self):
        record = build_decision_record(
            run_id="r1",
            account="personal",
            record=ApplyRecord("m1", "star", "ok"),
            decision=None,
            message=self._message(),
            log_subjects=False,
        )
        assert "subject" not in record

    def test_本文そのものはログに残らない(self):
        record = build_decision_record(
            run_id="r1",
            account="personal",
            record=ApplyRecord("m1", "star", "ok"),
            decision={"reason": "r"},
            message=self._message(),
            log_subjects=True,
        )
        assert "本文" not in json.dumps(record, ensure_ascii=False)
        assert record["body_chars"] == 20

    def test_rejectedのレコードはメッセージ情報なしでも作れる(self):
        record = build_decision_record(
            run_id="r1",
            account="personal",
            record=ApplyRecord("evil", "unknown", "rejected", "message_id が入力に存在しない"),
            decision=None,
            message=None,
            log_subjects=True,
        )
        assert record["applied"] == "rejected"
        assert record["partial_reason"] == "message_id が入力に存在しない"
