"""gmail の認証まわり（load_client_config / get_credentials）のテスト。ネットワークなし。"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from numa_inbox_zero.config import Config
from numa_inbox_zero.gmail import get_credentials, load_client_config

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


class TestGetCredentials:
    def _config(self, tmp_path) -> Config:
        cfg = Config(credentials_json=json.dumps(CLIENT_CONFIG))
        cfg.token_path = tmp_path / "tokens" / "default.json"
        cfg.token_path.parent.mkdir(parents=True)
        return cfg

    def test_refresh_tokenが失効していたらOAuthフローをやり直す(self, tmp_path):
        """OAuth 同意画面が「テスト」ステータスだとリフレッシュトークンが7日で
        失効し invalid_grant になる。auth コマンドがトレースバックで死なず、
        フローを再実行して新しいトークンを保存できることを保証する。"""
        cfg = self._config(tmp_path)
        cfg.token_path.write_text("{}", encoding="utf-8")

        stale = MagicMock(valid=False, expired=True, refresh_token="stale")
        stale.refresh.side_effect = RefreshError(
            "invalid_grant: Token has been expired or revoked."
        )
        fresh = MagicMock(valid=True)
        fresh.to_json.return_value = '{"token": "fresh"}'
        flow = MagicMock()
        flow.run_local_server.return_value = fresh

        with (
            patch(
                "numa_inbox_zero.gmail.Credentials.from_authorized_user_file",
                return_value=stale,
            ),
            patch(
                "numa_inbox_zero.gmail.InstalledAppFlow.from_client_config",
                return_value=flow,
            ),
        ):
            creds = get_credentials(cfg)

        assert creds is fresh
        flow.run_local_server.assert_called_once()
        # WSL で壊れたブラウザの自動起動を避ける（URL 手動オープン方式）
        assert flow.run_local_server.call_args.kwargs["open_browser"] is False
        assert cfg.token_path.read_text(encoding="utf-8") == '{"token": "fresh"}'

    def test_refreshが成功したらフローは起動しない(self, tmp_path):
        cfg = self._config(tmp_path)
        cfg.token_path.write_text("{}", encoding="utf-8")

        stale = MagicMock(valid=False, expired=True, refresh_token="ok")
        stale.to_json.return_value = '{"token": "refreshed"}'

        def refresh(_request):
            stale.valid = True

        stale.refresh.side_effect = refresh

        with (
            patch(
                "numa_inbox_zero.gmail.Credentials.from_authorized_user_file",
                return_value=stale,
            ),
            patch("numa_inbox_zero.gmail.InstalledAppFlow.from_client_config") as mock_flow,
        ):
            creds = get_credentials(cfg)

        assert creds is stale
        mock_flow.assert_not_called()
