import time

from langchain_groq import ChatGroq

from app.config import settings

MODEL_NAME = "openai/gpt-oss-20b"
MAX_RETRY_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 30.0


class AgentError(Exception):
    pass


def get_llm() -> ChatGroq:
    # request_timeout defaults to None (no bound at all) - a single stalled
    # network call could then hang indefinitely, and invoke_with_retry's
    # own retry loop never gets a chance to run since the first attempt
    # never returns. Confirmed by an actual hang during this project's
    # development. Always bound it.
    return ChatGroq(
        model=MODEL_NAME,
        groq_api_key=settings.groq_api_key,
        temperature=0,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
    )


def invoke_with_retry(model, messages):
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            return model.invoke(messages)
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRY_ATTEMPTS - 1:
                time.sleep(BASE_BACKOFF_SECONDS * (2**attempt))
    raise AgentError(f"LLM call failed after {MAX_RETRY_ATTEMPTS} attempts: {last_exc}") from last_exc
