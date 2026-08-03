"""config.py（複数アカウントのパス分離）のテスト。"""

from pathlib import Path

from numa_inbox_zero.config import Config


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
