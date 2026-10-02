import runpy
from unittest.mock import patch

from app.core.config import BACKEND_ROOT, Settings


def test_paths_do_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, database_url="sqlite:///./local.db", local_storage_path="./documents")
    assert settings.database_url == "sqlite:///" + (BACKEND_ROOT / "local.db").as_posix()
    assert settings.local_storage_path == str(BACKEND_ROOT / "documents")
    assert not (tmp_path / "local.db").exists()


def test_memory_database_and_absolute_storage_are_preserved(tmp_path):
    settings = Settings(_env_file=None, database_url="sqlite:///:memory:", local_storage_path=str(tmp_path))
    assert settings.database_url == "sqlite:///:memory:"
    assert settings.local_storage_path == str(tmp_path)


def test_server_settings_read_dotenv_and_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PORT=8123\nFORWARDED_ALLOW_IPS=10.0.0.1\n")
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    settings = Settings(_env_file=env_file)
    assert settings.port == 8123
    assert settings.forwarded_allow_ips == "10.0.0.1"
    monkeypatch.setenv("PORT", "8124")
    assert Settings(_env_file=env_file).port == 8124
    with patch("app.core.config.get_settings", return_value=settings), patch("uvicorn.run") as server:
        runpy.run_path(str(BACKEND_ROOT / "run.py"), run_name="__main__")
    assert server.call_args.kwargs["port"] == 8123
    assert server.call_args.kwargs["forwarded_allow_ips"] == "10.0.0.1"
