"""gmail.load_client_config（1Password env mount 対応）のテスト。"""

import json
from pathlib import Path

import pytest

from numa_inbox_zero.gmail import load_client_config

CLIENT_CONFIG = {
    "installed": {
        "client_id": "id.apps.googleusercontent.com",
        "client_secret": "secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


class TestLoadClientConfig:
    def test_env_JSONを優先して使う(self, tmp_path):
        # ファイルが存在してもenv側が勝つ
        path = tmp_path / "creds.json"
        path.write_text('{"installed": {"client_id": "file-side"}}', encoding="utf-8")
        config = load_client_config(json.dumps(CLIENT_CONFIG), path)
        assert config["installed"]["client_id"] == "id.apps.googleusercontent.com"

    def test_envが無ければファイルから読む(self, tmp_path):
        path = tmp_path / "creds.json"
        path.write_text(json.dumps(CLIENT_CONFIG), encoding="utf-8")
        config = load_client_config(None, path)
        assert config["installed"]["client_secret"] == "secret"

    def test_envの中身が壊れたJSONならValueError(self):
        with pytest.raises(ValueError, match="パースできない"):
            load_client_config("{not json", Path("/nonexistent"))

    def test_envの中身がOAuthクライアント形式でなければValueError(self):
        """op:// 参照の解決ミスで別のシークレットが入った場合に気づけるようにする。"""
        with pytest.raises(ValueError, match="installed"):
            load_client_config('{"api_key": "xxx"}', Path("/nonexistent"))

    def test_envもファイルも無ければFileNotFoundError(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="NIZ_CREDENTIALS_JSON"):
            load_client_config(None, tmp_path / "missing.json")

    def test_web形式のクライアント設定も受け付ける(self):
        config = load_client_config('{"web": {"client_id": "x"}}', Path("/nonexistent"))
        assert "web" in config
