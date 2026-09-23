"""The normal verification suite never inherits a developer's paid AI credentials."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_ai_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("OPENAI_") or name == "MONEY_GRAPH_ASSISTANT_TOKEN":
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        raise AssertionError("Normal tests must not call an external AI service")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
