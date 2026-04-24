import os
import anthropic

MODEL = "claude-sonnet-4-6"


class AnthropicClient:
    def __init__(self):
        self._client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def call(self, prompt: str, system: str, temperature: float = 0.3) -> str:
        response = self._client.messages.create(
            model=MODEL,
            max_tokens=512,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
