import logging
from collections.abc import Callable
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)

from app.agents.base import WorkspaceWriter
from app.agents.errors import AgentError
from app.config import Settings
from app.domain.models import AgentResult

logger = logging.getLogger(__name__)

ClientFactory = Callable[[Settings], Any]

MISSING_KEY_MESSAGE = "SumoPod is not configured. Set SUMOPOD_API_KEY on the backend."
TIMEOUT_MESSAGE = "The agent request timed out."
NETWORK_MESSAGE = "The agent provider could not be reached."
AUTH_MESSAGE = "The agent provider rejected the credentials."
RATE_LIMIT_MESSAGE = "The agent provider rate limit was reached."
PROVIDER_UNAVAILABLE_MESSAGE = "The agent provider is temporarily unavailable."
INVALID_RESPONSE_MESSAGE = "The agent returned an empty or invalid response."
PROVIDER_ERROR_MESSAGE = "The agent provider could not complete this request."


def _default_client_factory(settings: Settings) -> OpenAI:
    return OpenAI(
        api_key=settings.sumopod_api_key,
        base_url=settings.sumopod_base_url,
        timeout=settings.sumopod_timeout_seconds,
        max_retries=0,
    )


class SumoPodAgentAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or _default_client_factory

    def run(
        self,
        *,
        task_id: str,
        title: str,
        instruction: str,
        workspace: WorkspaceWriter,
    ) -> AgentResult:
        api_key = (self._settings.sumopod_api_key or "").strip()
        if not api_key:
            raise AgentError(MISSING_KEY_MESSAGE)

        title = title[:120]
        instruction = instruction[:4000]
        client = self._client_factory(self._settings)

        try:
            response = client.chat.completions.create(
                model=self._settings.sumopod_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a software engineering assistant. "
                            "Return clear text for the task. "
                            "Do not claim that you executed code or modified files."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Title: {title}\n\nInstruction:\n{instruction}",
                    },
                ],
            )
        except APITimeoutError:
            logger.error("sumopod request failed: APITimeoutError")
            raise AgentError(TIMEOUT_MESSAGE) from None
        except AuthenticationError:
            logger.error("sumopod request failed: AuthenticationError")
            raise AgentError(AUTH_MESSAGE) from None
        except PermissionDeniedError:
            logger.error("sumopod request failed: PermissionDeniedError")
            raise AgentError(AUTH_MESSAGE) from None
        except RateLimitError:
            logger.error("sumopod request failed: RateLimitError")
            raise AgentError(RATE_LIMIT_MESSAGE) from None
        except APIConnectionError:
            logger.error("sumopod request failed: APIConnectionError")
            raise AgentError(NETWORK_MESSAGE) from None
        except APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            logger.error("sumopod request failed: APIStatusError status=%s", status)
            if status in {401, 403}:
                raise AgentError(AUTH_MESSAGE) from None
            if status == 429:
                raise AgentError(RATE_LIMIT_MESSAGE) from None
            if status is not None and status >= 500:
                raise AgentError(PROVIDER_UNAVAILABLE_MESSAGE) from None
            raise AgentError(PROVIDER_ERROR_MESSAGE) from None
        except AgentError:
            raise
        except Exception:
            logger.error("sumopod request failed: %s", "UnexpectedError")
            raise AgentError(PROVIDER_ERROR_MESSAGE) from None

        content = self._extract_text(response)
        if not content:
            raise AgentError(INVALID_RESPONSE_MESSAGE)

        max_chars = self._settings.sumopod_max_output_chars
        if len(content) > max_chars:
            content = content[:max_chars]

        workspace.write_text(task_id, "result.txt", content)
        return AgentResult(output=content)

    def _extract_text(self, response: Any) -> str:
        try:
            choices = getattr(response, "choices", None)
            if not choices:
                return ""
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
            if content is None:
                return ""
            if isinstance(content, str):
                return content.strip()
            return str(content).strip()
        except Exception:
            logger.error("sumopod response parse failed: InvalidResponse")
            return ""
