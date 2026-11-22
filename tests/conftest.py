import pytest


@pytest.fixture(autouse=True)
def _no_real_session_keeper(monkeypatch):
    """Never let a test start the real session keeper (and a real browser).

    GEMINI_SESSION_PROFILE is set on a developer machine that runs the keeper;
    a test that reloads a file-sourced session would otherwise launch it.
    Tests that need it set it themselves.
    """
    monkeypatch.delenv("GEMINI_SESSION_PROFILE", raising=False)
