"""__main__.cmd_classify の統合テスト（Jev 呼び出しはモック、ネットワークなし）。"""

import argparse
import json
from unittest.mock import patch

from numa_inbox_zero.__main__ import cmd_classify
from numa_inbox_zero.classify import ClassifyError, ClassifyOutcome
from numa_inbox_zero.config import Config


def _make_config(tmp_path) -> Config:
    cfg = Config()
    cfg.work_dir = tmp_path / "work" / "default"
    cfg.logs_dir = tmp_path / "logs"
    cfg.ensure_dirs()
    return cfg


def _write_inbox(cfg: Config, count=1):
    messages = [
        {
            "message_id": f"m{i}",
            "thread_id": f"t{i}",
            "from": "shop@example.com",
            "subject": "件名",
            "body": "本文",
            "body_truncated": False,
        }
        for i in range(1, count + 1)
    ]
    cfg.inbox_path.write_text(
        json.dumps({"run_id": "r1", "query": "q", "messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )


def _decision(message_id):
    return {
        "message_id": message_id,
        "action": "archive",
        "category": "newsletter_promotion",
        "confidence": 0.95,
        "injection": 0.01,
        "reason": "r",
    }


class TestCmdClassify:
    def test_判定とメタデータがファイルに書かれる(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_inbox(cfg)
        outcome = ClassifyOutcome(
            decisions=[_decision("m1")],
            model="jev-1.13.0",
            usage={"input_tokens": 2000, "output_tokens": 20, "requests": 1},
            total_cost_usd=0.000084,
            duration_ms=180,
        )
        with patch(
            "numa_inbox_zero.__main__.classify_mod.run_jev_classifier", return_value=outcome
        ):
            assert cmd_classify(cfg, argparse.Namespace()) == 0

        classification = json.loads(cfg.classification_path.read_text(encoding="utf-8"))
        assert classification["decisions"][0]["message_id"] == "m1"
        meta = json.loads(cfg.classify_meta_path.read_text(encoding="utf-8"))
        assert meta["backend"] == "cloudflare-workers-ai"
        assert meta["usage"]["requests"] == 1

    def test_対象なしならAPIを呼ばない(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_inbox(cfg, count=0)
        with patch("numa_inbox_zero.__main__.classify_mod.run_jev_classifier") as mock_run:
            assert cmd_classify(cfg, argparse.Namespace()) == 0
        mock_run.assert_not_called()

    def test_一部失敗でも成功分を書いて終了コード0(self, tmp_path):
        """失敗した通は判定が無く Gmail 上に残るので、次回の実行で再試行される。"""
        cfg = _make_config(tmp_path)
        _write_inbox(cfg, count=2)
        outcome = ClassifyOutcome(
            decisions=[_decision("m1")],
            errors=[{"message_id": "m2", "detail": "RuntimeError: 429"}],
        )
        with patch(
            "numa_inbox_zero.__main__.classify_mod.run_jev_classifier", return_value=outcome
        ):
            assert cmd_classify(cfg, argparse.Namespace()) == 0

    def test_全通失敗なら終了コード1でapplyに進ませない(self, tmp_path):
        """空の判定で apply が走り「成功した空実行」としてログに残るのを防ぐ。"""
        cfg = _make_config(tmp_path)
        _write_inbox(cfg, count=2)
        outcome = ClassifyOutcome(
            decisions=[],
            errors=[
                {"message_id": "m1", "detail": "TypeSafeAuthenticationError: 401"},
                {"message_id": "m2", "detail": "TypeSafeAuthenticationError: 401"},
            ],
        )
        with patch(
            "numa_inbox_zero.__main__.classify_mod.run_jev_classifier", return_value=outcome
        ):
            assert cmd_classify(cfg, argparse.Namespace()) == 1

    def test_APIキー未設定はtracebackでなく1行のエラー(self, tmp_path, capsys):
        cfg = _make_config(tmp_path)
        _write_inbox(cfg)
        with patch(
            "numa_inbox_zero.__main__.classify_mod.run_jev_classifier",
            side_effect=ClassifyError("TypeSafe の API キーが無い"),
        ):
            assert cmd_classify(cfg, argparse.Namespace()) == 1
        assert "分類を実行できない" in capsys.readouterr().err
