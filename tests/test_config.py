"""config.py（複数アカウントのパス分離）のテスト。"""

from pathlib import Path

from numa_inbox_zero.config import (
    DEFAULT_MODEL,
    Config,
    accounts_from_env,
    poll_interval_seconds,
)


class TestAccountSeparation:
    def test_アカウントごとにトークンパスが分かれる(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("NIZ_TOKEN_DIR", raising=False)
        a = Config(account="personal")
        b = Config(account="work")
        assert a.token_path != b.token_path
        assert a.token_path.name == "personal.json"
        assert b.token_path.name == "work.json"

    def test_トークンはリポジトリ外のXDG_stateに置かれる(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("NIZ_TOKEN_DIR", raising=False)
        cfg = Config(account="personal")
        assert cfg.token_path == tmp_path / "numa-inbox-zero" / "tokens" / "personal.json"
        assert not cfg.token_path.is_relative_to(cfg.root)

    def test_NIZ_TOKEN_DIRで置き場所を変えられる(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NIZ_TOKEN_DIR", str(tmp_path / "custom"))
        cfg = Config(account="x")
        assert cfg.token_path == tmp_path / "custom" / "x.json"

    def test_アカウントごとに作業ディレクトリが分かれる(self):
        a = Config(account="personal")
        b = Config(account="work")
        assert a.work_dir != b.work_dir
        assert a.work_dir.name == "personal"
        assert a.inbox_path != b.inbox_path

    def test_ログは全アカウント共有(self):
        a = Config(account="personal")
        b = Config(account="work")
        assert a.runs_log_path == b.runs_log_path
        assert a.decisions_log_path == b.decisions_log_path


class TestCredentialsSource:
    def test_NIZ_CREDENTIALS_JSONが設定されていれば中身を持つ(self, monkeypatch):
        monkeypatch.setenv("NIZ_CREDENTIALS_JSON", '{"installed": {}}')
        cfg = Config()
        assert cfg.credentials_json == '{"installed": {}}'

    def test_未設定ならNone(self, monkeypatch):
        monkeypatch.delenv("NIZ_CREDENTIALS_JSON", raising=False)
        cfg = Config()
        assert cfg.credentials_json is None

    def test_NIZ_CREDENTIALSでファイルパスを指定できる(self, monkeypatch):
        monkeypatch.setenv("NIZ_CREDENTIALS", "/secret/place/creds.json")
        cfg = Config()
        assert cfg.credentials_path == Path("/secret/place/creds.json")


class TestDaemonSettings:
    def test_NIZ_ACCOUNTSをカンマ区切りで分解し空白を除去する(self, monkeypatch):
        monkeypatch.setenv("NIZ_ACCOUNTS", "personal, work ,private")
        assert accounts_from_env() == ["personal", "work", "private"]

    def test_NIZ_ACCOUNTS未設定なら空リスト(self, monkeypatch):
        monkeypatch.delenv("NIZ_ACCOUNTS", raising=False)
        assert accounts_from_env() == []

    def test_ポーリング間隔の既定は300秒(self, monkeypatch):
        monkeypatch.delenv("NIZ_POLL_INTERVAL", raising=False)
        assert poll_interval_seconds() == 300

    def test_NIZ_POLL_INTERVALで間隔を変えられる(self, monkeypatch):
        monkeypatch.setenv("NIZ_POLL_INTERVAL", "60")
        assert poll_interval_seconds() == 60


class TestTypeSafeSettings:
    def test_NIZ_TYPESAFE_API_KEYを読む(self, monkeypatch):
        monkeypatch.setenv("NIZ_TYPESAFE_API_KEY", "niz-key")
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        assert Config().typesafe_api_key == "niz-key"

    def test_SDK標準のTYPESAFE_API_KEYもフォールバックとして読む(self, monkeypatch):
        monkeypatch.delenv("NIZ_TYPESAFE_API_KEY", raising=False)
        monkeypatch.setenv("TYPESAFE_API_KEY", "sdk-key")
        assert Config().typesafe_api_key == "sdk-key"

    def test_NIZ側が優先される(self, monkeypatch):
        monkeypatch.setenv("NIZ_TYPESAFE_API_KEY", "niz-key")
        monkeypatch.setenv("TYPESAFE_API_KEY", "sdk-key")
        assert Config().typesafe_api_key == "niz-key"

    def test_未設定ならNone(self, monkeypatch):
        monkeypatch.delenv("NIZ_TYPESAFE_API_KEY", raising=False)
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        assert Config().typesafe_api_key is None

    def test_モデルの既定はバージョン固定(self, monkeypatch):
        """confidence の閾値はモデルバージョンに紐づくため alias を既定にしない。"""
        monkeypatch.delenv("NIZ_MODEL", raising=False)
        assert Config().model == DEFAULT_MODEL
        assert DEFAULT_MODEL != "jev-latest"

    def test_NIZ_MODELでモデルを変えられる(self, monkeypatch):
        monkeypatch.setenv("NIZ_MODEL", "jev-preview")
        assert Config().model == "jev-preview"

    def test_ゲートウェイ経由にするbase_urlは既定では未設定(self, monkeypatch):
        monkeypatch.delenv("NIZ_TYPESAFE_BASE_URL", raising=False)
        assert Config().typesafe_base_url is None

    def test_ポリシーはpolicy_tomlを指す(self):
        assert Config().policy_path.name == "policy.toml"
