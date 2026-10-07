import pytest

from reactor_render import config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("REACTOR_API_KEY", "REACTOR_API_URL", "REACTOR_LOCAL"):
        monkeypatch.delenv(var, raising=False)
    # Point at an empty directory so a real config.ini on this machine can't leak in.
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.ini"))


def test_missing_key_is_a_config_error_with_guidance():
    with pytest.raises(ValueError, match="REACTOR_API_KEY") as exc:
        config.connect_args()
    assert "config.ini" in str(exc.value)


def test_key_from_environment(monkeypatch):
    monkeypatch.setenv("REACTOR_API_KEY", "rk_test")
    assert config.connect_args() == {"api_key": "rk_test"}


def test_key_from_config_ini(tmp_path, monkeypatch):
    path = tmp_path / "config.ini"
    path.write_text("[API]\nREACTOR_API_KEY = rk_from_ini \n")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    assert config.connect_args() == {"api_key": "rk_from_ini"}


def test_local_mode_needs_no_key(monkeypatch):
    monkeypatch.setenv("REACTOR_LOCAL", "1")
    assert config.connect_args() == {"local": True}
    monkeypatch.setenv("REACTOR_API_URL", "http://localhost:9000")
    assert config.connect_args() == {"local": True, "api_url": "http://localhost:9000"}


def test_max_sessions_defaults_to_one():
    assert config.max_sessions() == 1
