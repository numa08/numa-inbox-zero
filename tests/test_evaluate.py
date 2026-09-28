"""evaluate.py（オフライン評価の中核ロジック）のテスト。"""

import json

import pytest

from numa_inbox_zero.evaluate import (
    build_judgments,
    build_result,
    config_hash,
    diff_results,
    golden_hash,
    import_candidates,
    load_golden,
    questions_hash,
    score_predictions,
    select_import_targets,
    split_labeled,
    sweep_thresholds,
)


def _golden(message_id="m1", expected="star", subject="件名"):
    return {
        "message": {"message_id": message_id, "subject": subject, "body": "本文"},
        "expected_action": expected,
    }


def _decision(message_id="m1", action="star", reason="r"):
    return {"message_id": message_id, "action": action, "reason": reason}


class TestLoadGolden:
    def test_存在しないファイルは空リスト(self, tmp_path):
        assert load_golden(tmp_path / "none.jsonl") == []

    def test_空行を無視して読む(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text(
            json.dumps(_golden()) + "\n\n" + json.dumps(_golden("m2")) + "\n",
            encoding="utf-8",
        )
        assert len(load_golden(path)) == 2

    def test_壊れた行は行番号付きでエラー(self, tmp_path):
        path = tmp_path / "golden.jsonl"
        path.write_text('{"ok": 1}\n{broken\n', encoding="utf-8")
        with pytest.raises(ValueError, match=":2"):
            load_golden(path)


class TestSplitLabeled:
    def test_expected_actionが有効なものだけラベル済み(self):
        entries = [
            _golden("m1", "star"),
            _golden("m2", None),
            _golden("m3", "delete"),  # enum 外は未ラベル扱い
        ]
        labeled, unlabeled = split_labeled(entries)
        assert [e["message"]["message_id"] for e in labeled] == ["m1"]
        assert unlabeled == 2

    def test_nullはsystem_actionへの同意として正解に採用される(self):
        """誤判定だけ上書きする運用。null + 有効な system_action は同意扱い。"""
        entries = [{**_golden("m1", None), "system_action": "archive"}]
        labeled, unlabeled = split_labeled(entries)
        assert labeled[0]["expected_action"] == "archive"
        assert unlabeled == 0

    def test_明示したexpected_actionはsystem_actionより優先される(self):
        entries = [{**_golden("m1", "star"), "system_action": "archive"}]
        labeled, _ = split_labeled(entries)
        assert labeled[0]["expected_action"] == "star"

    def test_nullでsystem_actionも無効なら未ラベル扱い(self):
        entries = [
            {**_golden("m1", None), "system_action": None},
            {**_golden("m2", None), "system_action": "send"},  # enum 外
            _golden("m3", None),  # system_action キー自体が無い
        ]
        labeled, unlabeled = split_labeled(entries)
        assert labeled == []
        assert unlabeled == 3

    def test_enum外のexpected_actionはsystem_actionにフォールバックしない(self):
        """タイポ（例: achive）を黙って同意扱いにすると誤ラベルが混入するため。"""
        entries = [{**_golden("m1", "achive"), "system_action": "archive"}]
        labeled, unlabeled = split_labeled(entries)
        assert labeled == []
        assert unlabeled == 1

    def test_正規化はコピーに対して行われ元のエントリを変更しない(self):
        entry = {**_golden("m1", None), "system_action": "archive"}
        split_labeled([entry])
        assert entry["expected_action"] is None


class TestSelectImportTargets:
    def _log(self, message_id, account="personal", applied="ok", action="archive"):
        return {
            "account": account,
            "message_id": message_id,
            "action": action,
            "applied": applied,
        }

    def test_指定アカウントの判定だけが対象になる(self):
        decisions = [self._log("m1"), self._log("m2", account="work")]
        targets = select_import_targets(decisions, account="personal")
        assert [t["message_id"] for t in targets] == ["m1"]

    def test_rejectedの判定は除外される(self):
        """検証で弾かれた行の message_id は捏造の可能性があり、Gmail 照会に回さない。"""
        decisions = [self._log("m1"), self._log("m2", applied="rejected")]
        targets = select_import_targets(decisions, account="personal")
        assert [t["message_id"] for t in targets] == ["m1"]

    def test_同じメールが複数回分類されていたら最新の判定を採用する(self):
        decisions = [self._log("m1", action="archive"), self._log("m1", action="star")]
        targets = select_import_targets(decisions, account="personal")
        assert len(targets) == 1
        assert targets[0]["action"] == "star"

    def test_message_idが欠けた行は無視される(self):
        decisions = [self._log(None), self._log("m1")]
        targets = select_import_targets(decisions, account="personal")
        assert [t["message_id"] for t in targets] == ["m1"]


class TestImportCandidates:
    def test_システム判定がsystem_actionとして残る(self):
        candidates = import_candidates(
            messages=[{"message_id": "m1", "subject": "s"}],
            decisions=[_decision("m1", "archive", "通知")],
            existing_ids=set(),
            imported_at="2026-08-03T09:00:00+09:00",
        )
        assert candidates[0]["expected_action"] is None
        assert candidates[0]["system_action"] == "archive"
        assert candidates[0]["system_reason"] == "通知"

    def test_既存エントリは取り込まない(self):
        """追記のみ・書き換えない原則。同じメールが二重登録されない。"""
        candidates = import_candidates(
            messages=[{"message_id": "m1"}, {"message_id": "m2"}],
            decisions=[],
            existing_ids={"m1"},
            imported_at="t",
        )
        assert [c["message"]["message_id"] for c in candidates] == ["m2"]


class TestScorePredictions:
    def test_全問正解(self):
        report = score_predictions([_golden()], [_decision()])
        assert report.accuracy == 1.0
        assert report.mismatches == []

    def test_不一致はmismatchesに理由付きで残る(self):
        report = score_predictions(
            [_golden("m1", "star")], [_decision("m1", "archive", "広告と判断")]
        )
        assert report.accuracy == 0.0
        assert report.mismatches[0]["expected"] == "star"
        assert report.mismatches[0]["got"] == "archive"
        assert report.mismatches[0]["model_reason"] == "広告と判断"

    def test_予測がないメールはmissingとして誤り扱い(self):
        """分類器の見逃しもスコアに現れる。"""
        report = score_predictions([_golden("m1", "archive")], [])
        assert report.confusion["archive"]["missing"] == 1
        assert report.archive_recall == 0.0

    def test_archive_precisionはfalse_archiveで下がる(self):
        golden = [
            _golden("m1", "archive"),
            _golden("m2", "star"),  # star を archive と誤判定 = false archive
        ]
        decisions = [_decision("m1", "archive"), _decision("m2", "archive")]
        report = score_predictions(golden, decisions)
        assert report.archive_precision == 0.5

    def test_archive_recallは取りこぼしで下がる(self):
        golden = [_golden("m1", "archive"), _golden("m2", "archive")]
        decisions = [_decision("m1", "archive"), _decision("m2", "star")]
        report = score_predictions(golden, decisions)
        assert report.archive_recall == 0.5

    def test_人間が明示したラベルだけを別集計する(self):
        """残りの行の正解は過去のモデルの判定なので、独立した信号を分けて見る。"""
        golden = [
            {**_golden("m1", "star"), "human_labeled": True},
            {**_golden("m2", "star"), "human_labeled": False},
        ]
        report = score_predictions(golden, [_decision("m1", "star"), _decision("m2", "archive")])
        assert report.human_labeled_total == 1
        assert report.human_labeled_correct == 1
        assert report.mismatches[0]["human_labeled"] is False

    def test_分母ゼロのメトリクスはNoneであり0ではない(self):
        # archive 予測ゼロ・archive 正解ゼロ
        report = score_predictions([_golden("m1", "star")], [_decision("m1", "star")])
        assert report.archive_precision is None
        assert report.archive_recall is None

    def test_予測はpredictionsに全件残る(self):
        report = score_predictions(
            [_golden("m1", "star"), _golden("m2", "archive")],
            [_decision("m1", "star")],
        )
        assert report.predictions == {"m1": "star", "m2": "missing"}


class TestHashes:
    def test_ラベル変更でgolden_hashが変わる(self):
        a = golden_hash([_golden("m1", "star")])
        b = golden_hash([_golden("m1", "archive")])
        assert a != b

    def test_並び順が違ってもgolden_hashは同じ(self):
        entries = [_golden("m1"), _golden("m2")]
        assert golden_hash(entries) == golden_hash(list(reversed(entries)))

    def test_questionsの文言が変わればquestions_hashが変わる(self):
        a = questions_hash({"category": {"criteria": {"x": "A"}}})
        b = questions_hash({"category": {"criteria": {"x": "B"}}})
        assert a != b

    def test_questions_hashはキー順に依存しない(self):
        a = questions_hash({"category": {"a": 1, "b": 2}})
        b = questions_hash({"category": {"b": 2, "a": 1}})
        assert a == b

    def test_ファイル内容の変更でconfig_hashが変わる(self, tmp_path):
        path = tmp_path / "policy.toml"
        path.write_text("v1", encoding="utf-8")
        h1 = config_hash([path])
        path.write_text("v2", encoding="utf-8")
        assert h1 != config_hash([path])


class TestDiffResults:
    def _result(self, name, predictions, archive_precision=0.9, cost=0.1):
        report = score_predictions([], [])
        result = build_result(
            name=name,
            model="jev-1.13.0",
            backend="jev",
            policy_hash="p1",
            questions="q1",
            golden="g1",
            thresholds={"archive_confidence": 0.85, "injection": 0.5},
            report=report,
            judgments={},
            cost_usd=cost,
            usage={},
            executed_at="t",
        )
        result["predictions"] = predictions
        result["metrics"]["archive_precision"] = archive_precision
        return result

    def test_判定が変わった件だけがchangedに出る(self):
        a = self._result("a", {"m1": "star", "m2": "archive"})
        b = self._result("b", {"m1": "star", "m2": "star"})
        diff = diff_results(a, b, {"m2": _golden("m2", "star")})
        assert len(diff["changed"]) == 1
        assert diff["changed"][0]["message_id"] == "m2"
        assert diff["changed"][0]["expected"] == "star"

    def test_メトリクス差分が計算される(self):
        a = self._result("a", {}, archive_precision=0.97)
        b = self._result("b", {}, archive_precision=0.93)
        diff = diff_results(a, b)
        assert diff["metrics"]["archive_precision"]["delta"] == -0.04

    def test_ゴールデンセットが違う比較には警告(self):
        a = self._result("a", {})
        b = self._result("b", {})
        b["golden_hash"] = "different"
        diff = diff_results(a, b)
        assert any("ゴールデンセット" in w for w in diff["warnings"])


class TestBuildJudgments:
    def test_降格前のactionと生の確率を残す(self):
        """閾値の掃引は保存済みの値だけで行えるようにする。"""
        judgments = build_judgments(
            [
                {
                    "message_id": "m1",
                    "action": "star",
                    "downgraded_from": "archive",
                    "category": "newsletter",
                    "confidence": 0.61,
                    "injection": 0.03,
                }
            ]
        )
        assert judgments["m1"]["proposed_action"] == "archive"
        assert judgments["m1"]["applied_action"] == "star"
        assert judgments["m1"]["confidence"] == 0.61


class TestSweepThresholds:
    def _judgment(self, confidence, injection=0.0):
        return {"proposed_action": "archive", "confidence": confidence, "injection": injection}

    def test_閾値を上げるとfalse_archiveが減る(self):
        labeled = [
            {**_golden("m1", "archive"), "human_labeled": True},
            {**_golden("m2", "star"), "human_labeled": True},
        ]
        judgments = {"m1": self._judgment(0.95), "m2": self._judgment(0.7)}
        rows = {
            r["archive_confidence"]: r
            for r in sweep_thresholds(labeled, judgments, injection_threshold=0.5)
        }
        assert rows[0.7]["false_archive"] == 1
        assert rows[0.95]["false_archive"] == 0
        assert rows[0.95]["archive_recall"] == 1.0

    def test_観測されたconfidenceが候補に含まれる(self):
        labeled = [_golden("m1", "archive")]
        rows = sweep_thresholds(labeled, {"m1": self._judgment(0.83)}, injection_threshold=0.5)
        assert 0.83 in [r["archive_confidence"] for r in rows]

    def test_injection閾値を超える判定はアーカイブされない(self):
        labeled = [_golden("m1", "archive")]
        rows = sweep_thresholds(
            labeled, {"m1": self._judgment(0.99, injection=0.9)}, injection_threshold=0.5
        )
        assert all(r["archived"] == 0 for r in rows)
