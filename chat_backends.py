from dataclasses import dataclass

from google.genai.types import HttpRetryOptions
from pydantic_ai import TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.ollama import OllamaProvider

@dataclass
class Turn:
    tool_calls: list[str]
    thoughts: str | None
    text: str | None
    input_tokens: int
    output_tokens: int
    truncated: bool = False

def make_model(backend, name, think, max_output_tokens, ollama_url, thinking_level, retry_attempts=None):
    if backend == "ollama":
        settings = OpenAIChatModelSettings(max_tokens=max_output_tokens)
        if not think:
            settings["openai_reasoning_effort"] = "none"
        return OllamaModel(name, provider=OllamaProvider(base_url=ollama_url)), settings
    # enough tries to wait out a per-minute limit, like the input tokens per minute
    provider = GoogleProvider(retry_options=HttpRetryOptions(attempts=retry_attempts))
    settings = GoogleModelSettings(google_thinking_config={"include_thoughts": True, "thinking_level": thinking_level})
    return GoogleModel(name, provider=provider), settings

def read_response(response):
    def joined(kind):
        return "\n".join(part.content for part in response.parts if isinstance(part, kind) and part.content) or None
    return Turn(
        tool_calls=[part.tool_name for part in response.parts if isinstance(part, ToolCallPart)],
        thoughts=joined(ThinkingPart),
        text=joined(TextPart),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        truncated=response.finish_reason == "length",
    )
