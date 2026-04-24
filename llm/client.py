import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    def call(self, prompt: str, system: str, temperature: float = 0.3) -> str: ...


def get_client() -> LLMClient:
    """
    Return the active LLM provider based on the LLM_PROVIDER env var.
    Defaults to 'anthropic'. Set LLM_PROVIDER=openai or LLM_PROVIDER=gemini
    to switch providers (see the corresponding stub in llm/providers/ first).
    """
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()

    if provider == "anthropic":
        from llm.providers.anthropic import AnthropicClient
        return AnthropicClient()
    elif provider == "openai":
        from llm.providers.openai import OpenAIClient
        return OpenAIClient()
    elif provider == "gemini":
        from llm.providers.gemini import GeminiClient
        return GeminiClient()
    else:
        raise ValueError(f"Unknown LLM_PROVIDER '{provider}'. Choose: anthropic, openai, gemini.")
