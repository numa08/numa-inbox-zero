"""__main__.cmd_apply の統合テスト（dry-run、ネットワークなし）。"""

import argparse
import json

from numa_inbox_zero.__main__ import cmd_apply
from numa_inbox_zero.config import Config


def _make_config(tmp_path, account="default") -> Config:
    cfg = Config(account=account)
    cfg.work_dir = tmp_path / "work" / account
    cfg.logs_dir = tmp_path / "logs"
    cfg.token_path = tmp_path / "tokens" / f"{account}.json"
    cfg.ensure_dirs()
    return cfg


def _write_work_files(cfg: Config, messages, decisions, meta=None):
    cfg.inbox_path.write_text(
        json.dumps({"run_id": "run-1", "query": "q", "messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )
    cfg.classification_path.write_text(
        json.dumps({"decisions": decisions}, ensure_ascii=False), encoding="utf-8"
    )
    if meta is not None:
        cfg.classify_meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _decision(message_id="m1", action="archive", confidence=0.95, **kw):
    base = {
        "message_id": message_id,
        "action": action,
        "category": "newsletter_promotion",
        "confidence": confidence,
        "injection": 0.01,
        "reason": "通知",
    }
    return {**base, **kw}


def _message(message_id="m1"):
    return {
        "message_id": message_id,
        "thread_id": f"t-{message_id}",
        "from": "taro@example.com",
        "subject": "件名",
        "body": "本文",
        "body_truncated": False,
    }


class TestCmdApplyDryRun:
    def test_dry_runで2つのログが書かれ終了コード0(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_work_files(
            cfg,
            [_message()],
            [_decision()],
            meta={"total_cost_usd": 0.01},
        )

        code = cmd_apply(cfg, argparse.Namespace(dry_run=True))

        assert code == 0
        run_record = json.loads(cfg.runs_log_path.read_text(encoding="utf-8"))
        assert run_record["mode"] == "dry-run"
        assert run_record["apply"]["archived"] == 1
        assert run_record["classify"]["total_cost_usd"] == 0.01

        decision_record = json.loads(cfg.decisions_log_path.read_text(encoding="utf-8"))
        assert decision_record["applied"] == "dry-run"
        assert decision_record["run_id"] == "run-1"

    def test_捏造message_idはdry_runでもrejectedになる(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_work_files(
            cfg,
            [_message()],
            [_decision(message_id="forged-id")],
        )

        code = cmd_apply(cfg, argparse.Namespace(dry_run=True))

        assert code == 0
        run_record = json.loads(cfg.runs_log_path.read_text(encoding="utf-8"))
        assert run_record["apply"]["rejected"] == 1
        assert run_record["apply"]["archived"] == 0

    def test_複数アカウントのログが共有JSONLにaccount付きで追記される(self, tmp_path):
        for account in ("personal", "work"):
            cfg = _make_config(tmp_path, account=account)
            _write_work_files(
                cfg,
                [_message()],
                [_decision()],
            )
            assert cmd_apply(cfg, argparse.Namespace(dry_run=True)) == 0

        lines = (tmp_path / "logs" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        accounts = [json.loads(line)["account"] for line in lines]
        assert accounts == ["personal", "work"]

        decision_lines = (
            (tmp_path / "logs" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        )
        assert [json.loads(line)["account"] for line in decision_lines] == ["personal", "work"]

    def test_確信度が閾値未満のarchiveはstarに降格されログに残る(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_work_files(cfg, [_message()], [_decision(confidence=0.5)])

        code = cmd_apply(cfg, argparse.Namespace(dry_run=True))

        assert code == 0
        run_record = json.loads(cfg.runs_log_path.read_text(encoding="utf-8"))
        assert run_record["apply"]["archived"] == 0
        assert run_record["apply"]["starred"] == 1
        assert run_record["apply"]["downgraded"] == 1

        decision_record = json.loads(cfg.decisions_log_path.read_text(encoding="utf-8"))
        assert decision_record["action"] == "star"
        assert decision_record["downgraded_from"] == "archive"
        assert decision_record["confidence"] == 0.5

    def test_confidence欠損のarchiveも降格される(self, tmp_path):
        """API が確率を返さなかった場合でも安全側に倒れる。"""
        cfg = _make_config(tmp_path)
        decision = _decision()
        del decision["confidence"]
        _write_work_files(cfg, [_message()], [decision])

        assert cmd_apply(cfg, argparse.Namespace(dry_run=True)) == 0
        run_record = json.loads(cfg.runs_log_path.read_text(encoding="utf-8"))
        assert run_record["apply"]["archived"] == 0
        assert run_record["apply"]["downgraded"] == 1

    def test_インジェクションが疑われるarchiveも降格される(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_work_files(cfg, [_message()], [_decision(injection=0.95)])

        assert cmd_apply(cfg, argparse.Namespace(dry_run=True)) == 0
        run_record = json.loads(cfg.runs_log_path.read_text(encoding="utf-8"))
        assert run_record["apply"]["archived"] == 0
        assert run_record["apply"]["starred"] == 1
        assert run_record["apply"]["downgraded"] == 1

    def test_policyに無いカテゴリはrejectedになる(self, tmp_path):
        """モデルの応答と policy.toml がずれても Gmail は変更されない。"""
        cfg = _make_config(tmp_path)
        _write_work_files(cfg, [_message()], [_decision(action=None, category="not_in_policy")])

        assert cmd_apply(cfg, argparse.Namespace(dry_run=True)) == 0
        run_record = json.loads(cfg.runs_log_path.read_text(encoding="utf-8"))
        assert run_record["apply"]["rejected"] == 1
        assert run_record["apply"]["archived"] == 0

    def test_decisionsが入力件数を超えたら中断して終了コード1(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_work_files(
            cfg,
            [_message()],
            [_decision("m1"), _decision("m2")],
        )

        code = cmd_apply(cfg, argparse.Namespace(dry_run=True))

        assert code == 1
        run_record = json.loads(cfg.runs_log_path.read_text(encoding="utf-8"))
        assert run_record["mode"] == "error"
        # 適用ログ（decisions.jsonl）には何も書かれない
        assert not cfg.decisions_log_path.exists()
