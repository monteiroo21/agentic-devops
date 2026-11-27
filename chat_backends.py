import os
from dataclasses import dataclass, field

from google.genai.types import HttpRetryOptions
from openai import AsyncOpenAI
from pydantic_ai import TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
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
    tool_args: list[str] = field(default_factory=list)  # each tool call's arguments, as the model wrote them

def make_model(backend, name, think, max_output_tokens, ollama_url, thinking_level, retry_attempts=None,
               ollama_timeout_s=None, ollama_temperature=None, ollama_top_p=None):
    if backend == "ollama":
        # a long reply on a CPU can outlast the client's default timeout of 600 s, so the caller sets its own
        settings = OpenAIChatModelSettings(max_tokens=max_output_tokens)
        if ollama_timeout_s:
            settings["timeout"] = ollama_timeout_s
        if ollama_temperature is not None:
            settings["temperature"] = ollama_temperature
        if ollama_top_p is not None:
            settings["top_p"] = ollama_top_p
        if not think:
            settings["openai_reasoning_effort"] = "none"
        client = AsyncOpenAI(base_url=ollama_url, api_key=os.getenv("OLLAMA_API_KEY") or "api-key-not-set",
                             max_retries=0)
        profile = OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False)
        return OllamaModel(name, provider=OllamaProvider(openai_client=client), profile=profile), settings
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
        tool_args=[part.args_as_json_str() for part in response.parts if isinstance(part, ToolCallPart)],
    )
