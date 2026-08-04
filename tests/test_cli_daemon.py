"""__main__ の daemon モードのテスト（ネットワークなし）。"""

import argparse
from unittest import mock

from numa_inbox_zero import __main__ as cli
from numa_inbox_zero.config import Config


def _make_config_factory(tmp_path):
    def factory(account="default"):
        cfg = Config(account=account)
        cfg.work_dir = tmp_path / "work" / account
        cfg.logs_dir = tmp_path / "logs"
        cfg.token_path = tmp_path / "tokens" / f"{account}.json"
        return cfg

    return factory


def _args(dry_run=True, interval=300):
    return argparse.Namespace(dry_run=dry_run, interval=interval)


def _inbox(n):
    return {
        "run_id": "run-1",
        "query": "q",
        "messages": [{"message_id": f"m{i}"} for i in range(n)],
    }


class TestDaemonCycle:
    def test_新着ゼロのアカウントはclassifyとapplyに進まない(self, tmp_path):
        with (
            mock.patch.object(cli, "_fetch_inbox", return_value=_inbox(0)) as fetch,
            mock.patch.object(cli, "cmd_classify") as classify,
            mock.patch.object(cli, "cmd_apply") as apply_,
        ):
            cli._daemon_cycle(["a"], _args(), config_factory=_make_config_factory(tmp_path))

        fetch.assert_called_once()
        classify.assert_not_called()
        apply_.assert_not_called()

    def test_新着があればclassifyとapplyが呼ばれる(self, tmp_path):
        with (
            mock.patch.object(cli, "_fetch_inbox", return_value=_inbox(2)),
            mock.patch.object(cli, "cmd_classify", return_value=0) as classify,
            mock.patch.object(cli, "cmd_apply") as apply_,
        ):
            cli._daemon_cycle(["a"], _args(), config_factory=_make_config_factory(tmp_path))

        classify.assert_called_once()
        apply_.assert_called_once()

    def test_classify失敗時はapplyに進まない(self, tmp_path):
        with (
            mock.patch.object(cli, "_fetch_inbox", return_value=_inbox(1)),
            mock.patch.object(cli, "cmd_classify", return_value=1),
            mock.patch.object(cli, "cmd_apply") as apply_,
        ):
            cli._daemon_cycle(["a"], _args(), config_factory=_make_config_factory(tmp_path))

        apply_.assert_not_called()

    def test_1アカウントの失敗で残りのアカウントは止まらない(self, tmp_path, capsys):
        processed = []

        def fetch(cfg):
            if cfg.account == "a":
                raise RuntimeError("API 障害")
            processed.append(cfg.account)
            return _inbox(0)

        with mock.patch.object(cli, "_fetch_inbox", side_effect=fetch):
            cli._daemon_cycle(["a", "b"], _args(), config_factory=_make_config_factory(tmp_path))

        assert processed == ["b"]
        assert "API 障害" in capsys.readouterr().err


class TestCmdDaemon:
    def test_sleep中のkeyboard_interruptで正常終了する(self, tmp_path):
        with (
            mock.patch.object(cli, "_daemon_cycle") as cycle,
            mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            code = cli.cmd_daemon(_make_config_factory(tmp_path)(), _args())

        assert code == 0
        cycle.assert_called_once()

    def test_NIZ_ACCOUNTSの全アカウントを巡回する(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NIZ_ACCOUNTS", "personal, work")
        with (
            mock.patch.object(cli, "_daemon_cycle") as cycle,
            mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            cli.cmd_daemon(_make_config_factory(tmp_path)(), _args())

        assert cycle.call_args[0][0] == ["personal", "work"]

    def test_NIZ_ACCOUNTS未設定ならaccount引数のアカウントのみ(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NIZ_ACCOUNTS", raising=False)
        with (
            mock.patch.object(cli, "_daemon_cycle") as cycle,
            mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            cli.cmd_daemon(_make_config_factory(tmp_path)(account="solo"), _args())

        assert cycle.call_args[0][0] == ["solo"]
