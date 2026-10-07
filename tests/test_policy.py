"""policy.py（policy.toml の読み込み）のテスト。

リポジトリの policy.toml 自体も検証する。ここが壊れると分類が全件止まるため、
テストで壊れたまま入らないようにする。
"""

import pytest

from numa_inbox_zero.config import PROJECT_ROOT
from numa_inbox_zero.policy import VALID_ACTIONS, PolicyError, load_policy

SAMPLE = """
[thresholds]
archive_confidence = 0.8
injection = 0.4

[[category]]
name = "newsletter"
action = "archive"
description = "Newsletter"

[[category]]
name = "other"
action = "star"
description = "None of the above"
"""


def _write(tmp_path, text):
    path = tmp_path / "policy.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadPolicy:
    def test_カテゴリと閾値を読む(self, tmp_path):
        policy = load_policy(_write(tmp_path, SAMPLE))
        assert policy.criteria == {"newsletter": "Newsletter", "other": "None of the above"}
        assert policy.thresholds.archive_confidence == 0.8
        assert policy.thresholds.injection == 0.4

    def test_action_forでカテゴリからactionを引ける(self, tmp_path):
        policy = load_policy(_write(tmp_path, SAMPLE))
        assert policy.action_for("newsletter") == "archive"
        assert policy.action_for("other") == "star"
        assert policy.action_for("unknown") is None

    def test_不正なactionはPolicyError(self, tmp_path):
        text = SAMPLE.replace('action = "archive"', 'action = "reply"')
        with pytest.raises(PolicyError, match="action が不正"):
            load_policy(_write(tmp_path, text))

    def test_カテゴリ名の重複はPolicyError(self, tmp_path):
        text = SAMPLE + '\n[[category]]\nname = "other"\naction = "star"\ndescription = "dup"\n'
        with pytest.raises(PolicyError, match="重複"):
            load_policy(_write(tmp_path, text))

    def test_descriptionが無ければPolicyError(self, tmp_path):
        text = SAMPLE.replace('description = "Newsletter"', "")
        with pytest.raises(PolicyError, match="必須"):
            load_policy(_write(tmp_path, text))

    def test_カテゴリが空ならPolicyError(self, tmp_path):
        with pytest.raises(PolicyError, match="1 件もない"):
            load_policy(_write(tmp_path, "[thresholds]\narchive_confidence = 0.8\ninjection = 0.4"))

    def test_閾値が範囲外ならPolicyError(self, tmp_path):
        text = SAMPLE.replace("archive_confidence = 0.8", "archive_confidence = 1.5")
        with pytest.raises(PolicyError, match="0..1"):
            load_policy(_write(tmp_path, text))

    def test_閾値が欠けていればPolicyError(self, tmp_path):
        text = SAMPLE.replace("injection = 0.4", "")
        with pytest.raises(PolicyError, match="injection"):
            load_policy(_write(tmp_path, text))

    def test_TOMLとして壊れていればPolicyError(self, tmp_path):
        with pytest.raises(PolicyError, match="TOML"):
            load_policy(_write(tmp_path, "[[category]\nname ="))


class TestRepositoryPolicy:
    """リポジトリに入っている policy.toml 自体の検証。"""

    def _policy(self):
        return load_policy(PROJECT_ROOT / "policy.toml")

    def test_読み込める(self):
        assert len(self._policy().categories) > 1

    def test_全カテゴリのactionがarchiveかstar(self):
        assert {c.action for c in self._policy().categories} <= VALID_ACTIONS

    def test_otherカテゴリがあり安全側のstarになっている(self):
        """Choice には必ず逃げ道を用意し、そこに落ちたものは人の目に入れる。"""
        assert self._policy().action_for("other") == "star"

    def test_Choiceのオプション上限を超えていない(self):
        assert len(self._policy().categories) <= 255
