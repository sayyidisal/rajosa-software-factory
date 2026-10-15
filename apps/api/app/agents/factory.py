from app.agents.base import AgentAdapter
from app.agents.mock import MockAgentAdapter
from app.agents.sumopod import SumoPodAgentAdapter
from app.config import Settings


def normalize_adapter_name(value: str) -> str:
    return value.strip().lower()


def build_agent_adapter(settings: Settings) -> AgentAdapter:
    mode = normalize_adapter_name(settings.agent_adapter)
    if mode == "mock":
        return MockAgentAdapter()
    if mode == "sumopod":
        # Always return SumoPod when explicitly requested. Missing API key fails at run time.
        return SumoPodAgentAdapter(settings)
    raise ValueError(
        f"Unknown AGENT_ADAPTER={mode!r}. Supported values: mock, sumopod."
    )
