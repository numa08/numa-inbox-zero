"""eval サブコマンドの統合テスト（Jev 呼び出しはモック、ネットワークなし）。"""

import argparse
import json
from unittest.mock import patch

from numa_inbox_zero.__main__ import (
    cmd_eval_diff,
    cmd_eval_import,
    cmd_eval_run,
    cmd_eval_sweep,
)
from numa_inbox_zero.classify import ClassifyOutcome
from numa_inbox_zero.config import Config


def _make_config(tmp_path) -> Config:
    cfg = Config()
    cfg.root = tmp_path
    cfg.work_dir = tmp_path / "work" / "default"
    cfg.logs_dir = tmp_path / "logs"
    cfg.ensure_dirs()
    # eval_dir 等は root から導出されるプロパティなので root の差し替えで切り替わる
    return cfg


def _write_golden(cfg: Config, entries):
    cfg.eval_dir.mkdir(parents=True, exist_ok=True)
    cfg.golden_path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


def _golden(message_id, expected):
    return {
        "message": {"message_id": message_id, "subject": f"件名{message_id}", "body": "本文"},
        "expected_action": expected,
    }


def _outcome(decisions, cost=0.05):
    return ClassifyOutcome(
        decisions=decisions,
        model="jev-1.13.0",
        usage={"input_tokens": 100},
        total_cost_usd=cost,
    )


def _judgment(message_id, action="star", confidence=0.95, injection=0.01):
    return {
        "message_id": message_id,
        "action": action,
        "category": "newsletter_promotion" if action == "archive" else "invoice_payment",
        "confidence": confidence,
        "injection": injection,
        "reason": "r",
    }


def _write_decisions(cfg: Config, records):
    cfg.decisions_log_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.decisions_log_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def _decision_log(message_id, action="archive", account="default", applied="ok"):
    return {
        "run_id": "r1",
        "account": account,
        "message_id": message_id,
        "action": action,
        "reason": "通知",
        "applied": applied,
    }


class TestEvalImport:
    def test_実行ログとgmail取得の合成で候補を追記する(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_decisions(cfg, [_decision_log("m1")])

        with (
            patch("numa_inbox_zero.__main__.gmail.get_service") as mock_service,
            patch(
                "numa_inbox_zero.__main__.gmail.fetch_messages_by_ids",
                return_value=([{"message_id": "m1", "subject": "s", "body": "b"}], []),
            ) as mock_fetch,
        ):
            assert cmd_eval_import(cfg, argparse.Namespace()) == 0

        # ログ由来の message_id が Gmail 照会に渡っている
        assert mock_fetch.call_args.args[1] == ["m1"]
        mock_service.assert_called_once()

        entry = json.loads(cfg.golden_path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["expected_action"] is None
        assert entry["system_action"] == "archive"
        assert entry["system_reason"] == "通知"
        assert entry["message"]["body"] == "b"
        assert entry["note"] == "account=default"

    def test_取り込み済みメールはgmailに照会せず二重取り込みしない(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_decisions(cfg, [_decision_log("m1")])
        _write_golden(cfg, [_golden("m1", None)])

        with patch("numa_inbox_zero.__main__.gmail.get_service") as mock_service:
            assert cmd_eval_import(cfg, argparse.Namespace()) == 0

        mock_service.assert_not_called()
        assert len(cfg.golden_path.read_text(encoding="utf-8").splitlines()) == 1

    def test_取得できなかったメールは候補に含めず正常終了する(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_decisions(cfg, [_decision_log("m1"), _decision_log("m2")])

        with (
            patch("numa_inbox_zero.__main__.gmail.get_service"),
            patch(
                "numa_inbox_zero.__main__.gmail.fetch_messages_by_ids",
                return_value=([{"message_id": "m1", "subject": "s", "body": "b"}], ["m2"]),
            ),
        ):
            assert cmd_eval_import(cfg, argparse.Namespace()) == 0

        lines = cfg.golden_path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["message"]["message_id"] for line in lines] == ["m1"]

    def test_対象アカウントの判定がなければgmailに触れずエラー終了(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_decisions(cfg, [_decision_log("m1", account="other")])

        with patch("numa_inbox_zero.__main__.gmail.get_service") as mock_service:
            assert cmd_eval_import(cfg, argparse.Namespace()) == 1
        mock_service.assert_not_called()

    def test_ログ自体がなければエラー終了(self, tmp_path):
        cfg = _make_config(tmp_path)
        assert cmd_eval_import(cfg, argparse.Namespace()) == 1


class TestEvalRun:
    def test_採点結果がファイルに保存される(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_golden(cfg, [_golden("m1", "star"), _golden("m2", "archive")])

        outcome = _outcome(
            [
                _judgment("m1", "star"),
                _judgment("m2", "star"),  # 誤り
            ]
        )
        with patch(
            "numa_inbox_zero.__main__.classify_mod.run_jev_classifier",
            return_value=outcome,
        ) as mock_run:
            code = cmd_eval_run(cfg, argparse.Namespace(name="exp1", model=None))

        assert code == 0
        # ゴールデンのメールが分類器に渡っている
        passed = mock_run.call_args.kwargs["messages"]
        assert [m["message_id"] for m in passed] == ["m1", "m2"]

        result = json.loads((cfg.eval_results_dir / "exp1.json").read_text(encoding="utf-8"))
        assert result["metrics"]["accuracy"] == 0.5
        assert result["golden_count"] == 2
        assert result["predictions"] == {"m1": "star", "m2": "star"}
        assert result["policy_hash"]
        assert result["questions_hash"]
        assert result["backend"] == "cloudflare-workers-ai"
        assert result["thresholds"]["archive_confidence"] > 0

    def test_採点は本番と同じゲートを通した結果で行われる(self, tmp_path):
        """confidence が閾値未満の archive は評価でも star として採点される。"""
        cfg = _make_config(tmp_path)
        _write_golden(cfg, [_golden("m1", "archive")])

        with patch(
            "numa_inbox_zero.__main__.classify_mod.run_jev_classifier",
            return_value=_outcome([_judgment("m1", "archive", confidence=0.1)]),
        ):
            assert cmd_eval_run(cfg, argparse.Namespace(name="gated", model=None)) == 0

        result = json.loads((cfg.eval_results_dir / "gated.json").read_text(encoding="utf-8"))
        assert result["predictions"] == {"m1": "star"}
        # 掃引できるよう降格前の判定と生の confidence が残る
        assert result["judgments"]["m1"]["proposed_action"] == "archive"
        assert result["judgments"]["m1"]["confidence"] == 0.1

    def test_未ラベルのみならエラー終了(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_golden(cfg, [_golden("m1", None)])
        code = cmd_eval_run(cfg, argparse.Namespace(name="x", model=None))
        assert code == 1

    def test_modelフラグでモデルを上書きできる(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_golden(cfg, [_golden("m1", "star")])

        with patch(
            "numa_inbox_zero.__main__.classify_mod.run_jev_classifier",
            return_value=_outcome([_judgment("m1", "star")]),
        ) as mock_run:
            cmd_eval_run(cfg, argparse.Namespace(name="h1", model="jev-preview"))
        assert mock_run.call_args.kwargs["model"] == "jev-preview"


class TestEvalSweep:
    def _result(self, cfg, name, judgments):
        cfg.eval_results_dir.mkdir(parents=True, exist_ok=True)
        (cfg.eval_results_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "thresholds": {"archive_confidence": 0.85, "injection": 0.5},
                    "judgments": judgments,
                }
            ),
            encoding="utf-8",
        )

    def test_保存済み結果からAPIを呼ばずに掃引する(self, tmp_path, capsys):
        cfg = _make_config(tmp_path)
        _write_golden(cfg, [_golden("m1", "archive"), _golden("m2", "star")])
        self._result(
            cfg,
            "exp1",
            {
                "m1": {"proposed_action": "archive", "confidence": 0.95, "injection": 0.0},
                "m2": {"proposed_action": "archive", "confidence": 0.7, "injection": 0.0},
            },
        )

        assert cmd_eval_sweep(cfg, argparse.Namespace(name="exp1", injection=None)) == 0
        out = capsys.readouterr().out
        assert "false_arch" in out
        assert "0.95" in out

    def test_judgmentsが無い結果ファイルはエラー終了(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_golden(cfg, [_golden("m1", "archive")])
        cfg.eval_results_dir.mkdir(parents=True, exist_ok=True)
        (cfg.eval_results_dir / "old.json").write_text(json.dumps({"name": "old"}), "utf-8")
        assert cmd_eval_sweep(cfg, argparse.Namespace(name="old", injection=None)) == 1

    def test_結果ファイルがなければエラー終了(self, tmp_path):
        cfg = _make_config(tmp_path)
        assert cmd_eval_sweep(cfg, argparse.Namespace(name="none", injection=None)) == 1


class TestEvalDiff:
    def test_2つの結果を比較して終了コード0(self, tmp_path, capsys):
        cfg = _make_config(tmp_path)
        _write_golden(cfg, [_golden("m1", "star")])
        cfg.eval_results_dir.mkdir(parents=True, exist_ok=True)
        base = {
            "name": "a",
            "golden_hash": "g",
            "metrics": {"archive_precision": 0.97, "archive_recall": 0.8, "accuracy": 0.9},
            "cost_usd": 0.3,
            "predictions": {"m1": "star"},
        }
        challenger = dict(
            base,
            name="b",
            metrics={"archive_precision": 0.93, "archive_recall": 0.8, "accuracy": 0.9},
            predictions={"m1": "archive"},
        )
        (cfg.eval_results_dir / "a.json").write_text(json.dumps(base), encoding="utf-8")
        (cfg.eval_results_dir / "b.json").write_text(json.dumps(challenger), encoding="utf-8")

        code = cmd_eval_diff(cfg, argparse.Namespace(a="a", b="b"))
        assert code == 0
        out = capsys.readouterr().out
        assert "危険な劣化" in out  # archive_precision の低下が警告される
        assert "m1" in out  # 判定が変わった件が表示される

    def test_結果ファイルがなければエラー終了(self, tmp_path):
        cfg = _make_config(tmp_path)
        assert cmd_eval_diff(cfg, argparse.Namespace(a="none1", b="none2")) == 1
