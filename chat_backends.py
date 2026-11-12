import os
from dataclasses import dataclass

from google.genai.types import HttpRetryOptions
from openai import AsyncOpenAI
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

def make_model(backend, name, think, max_output_tokens, ollama_url, thinking_level, retry_attempts=None,
               ollama_timeout_s=None):
    if backend == "ollama":
        # a long reply on a CPU can outlast the client's default timeout of 600 s, so the caller sets its own
        settings = OpenAIChatModelSettings(max_tokens=max_output_tokens)
        if ollama_timeout_s:
            settings["timeout"] = ollama_timeout_s
        if not think:
            settings["openai_reasoning_effort"] = "none"
        # the OpenAI client sends a request again, up to twice, when it times out or fails, and pydantic-ai keeps its
        # default. A request that timed out would only time out again on this CPU, so it fails at once instead
        client = AsyncOpenAI(base_url=ollama_url, api_key=os.getenv("OLLAMA_API_KEY") or "api-key-not-set",
                             max_retries=0)
        return OllamaModel(name, provider=OllamaProvider(openai_client=client)), settings
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
