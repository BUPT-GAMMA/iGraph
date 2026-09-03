from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.runtime import dotenv_compat
from backend.runtime.env_required import require_any_env, require_env, require_int_env
from backend.runtime.proxy_compat import setup_no_proxy


def test_env_requirements_strip_values_and_report_invalid_input(monkeypatch) -> None:
    monkeypatch.setenv("TEXT_VALUE", "  ready  ")
    monkeypatch.setenv("INT_VALUE", " 42 ")
    monkeypatch.setenv("EMPTY_VALUE", "   ")

    assert require_env("TEXT_VALUE") == "ready"
    assert require_int_env("INT_VALUE") == 42
    assert require_any_env("EMPTY_VALUE", "TEXT_VALUE") == "ready"

    monkeypatch.delenv("MISSING_VALUE", raising=False)
    with pytest.raises(ValueError, match="MISSING_VALUE"):
        require_env("MISSING_VALUE")
    with pytest.raises(ValueError, match="EMPTY_VALUE, MISSING_VALUE"):
        require_any_env("EMPTY_VALUE", "MISSING_VALUE")
    monkeypatch.setenv("INT_VALUE", "forty-two")
    with pytest.raises(ValueError, match="必须是整数"):
        require_int_env("INT_VALUE")


def test_fallback_dotenv_parser_handles_exports_quotes_and_existing_values(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "export EXPORTED = 'yes'",
                'QUOTED="hello world"',
                "PLAIN=value=with=equals",
                "EXISTING=from-file",
                "MALFORMED",
                "=missing-key",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXISTING", "from-environment")

    assert dotenv_compat._load_env_file(env_file) is True
    assert os.environ["EXPORTED"] == "yes"
    assert os.environ["QUOTED"] == "hello world"
    assert os.environ["PLAIN"] == "value=with=equals"
    assert os.environ["EXISTING"] == "from-environment"
    assert dotenv_compat._load_env_file(tmp_path / "missing") is False


def test_load_dotenv_compat_uses_library_then_falls_back_to_cwd(tmp_path: Path, monkeypatch) -> None:
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda: True)
    assert dotenv_compat.load_dotenv_compat() is True

    def fail_load() -> bool:
        raise RuntimeError("synthetic import failure")

    monkeypatch.setattr(dotenv, "load_dotenv", fail_load)
    monkeypatch.chdir(tmp_path)
    tmp_path.joinpath(".env").write_text("FALLBACK_VALUE=loaded\n", encoding="utf-8")
    monkeypatch.delenv("FALLBACK_VALUE", raising=False)
    assert dotenv_compat.load_dotenv_compat() is True
    assert os.environ["FALLBACK_VALUE"] == "loaded"


def test_no_proxy_setup_adds_local_hosts_without_duplicates(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "example.com, localhost")
    monkeypatch.setenv("no_proxy", "")

    setup_no_proxy()
    setup_no_proxy()

    assert os.environ["NO_PROXY"].split(",") == ["example.com", "localhost", "127.0.0.1"]
    assert os.environ["no_proxy"].split(",") == ["localhost", "127.0.0.1"]
