import pytest
from langchain_groq import ChatGroq

from app.llm import AgentError, get_llm, invoke_with_retry


def test_get_llm_returns_chat_groq_instance():
    assert isinstance(get_llm(), ChatGroq)


def test_invoke_with_retry_succeeds_after_transient_failures(monkeypatch):
    monkeypatch.setattr("app.llm.time.sleep", lambda _seconds: None)

    calls = {"count": 0}

    class _FlakyModel:
        def invoke(self, messages):
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("transient 503")
            return "ok"

    result = invoke_with_retry(_FlakyModel(), messages=["hi"])
    assert result == "ok"
    assert calls["count"] == 3


def test_invoke_with_retry_raises_agent_error_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr("app.llm.time.sleep", lambda _seconds: None)

    class _AlwaysFailsModel:
        def invoke(self, messages):
            raise RuntimeError("permanent failure")

    with pytest.raises(AgentError, match="permanent failure"):
        invoke_with_retry(_AlwaysFailsModel(), messages=["hi"])
