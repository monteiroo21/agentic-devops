import json
from dataclasses import dataclass

import ollama
from google import genai
from google.genai import types

@dataclass
class ToolCall:
    name: str
    args: dict

@dataclass
class Turn:
    tool_calls: list[ToolCall]
    thoughts: str | None
    input_tokens: int
    output_tokens: int
    truncated: bool = False

class GeminiChat:
    def __init__(self, model, system_prompt, tools):
        self.model = model
        self.client = genai.Client(
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions())
        )
        self.config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=tools)],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            ),
            thinking_config=types.ThinkingConfig(include_thoughts=True, thinking_level="HIGH"),
        )
        self.contents = []

    def add_user_message(self, message):
        self.contents.append(types.Content(role="user", parts=[types.Part(text=message)]))

    def generate(self):
        response = self.client.models.generate_content(
            model=self.model,
            contents=self.contents,
            config=self.config
        )
        content = response.candidates[0].content
        self.contents.append(content)

        thoughts = [part.text for part in content.parts or [] if part.thought and part.text]
        usage = response.usage_metadata or types.GenerateContentResponseUsageMetadata()
        return Turn(
            tool_calls=[ToolCall(call.name, dict(call.args or {})) for call in response.function_calls or []],
            thoughts="\n".join(thoughts) or None,
            input_tokens=usage.prompt_token_count or 0,
            output_tokens=(usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
            truncated=response.candidates[0].finish_reason == types.FinishReason.MAX_TOKENS,
        )

    def add_tool_results(self, results):
        parts = [types.Part.from_function_response(name=call.name, response={"result": result})
                 for call, result in results]
        self.contents.append(types.Content(role="user", parts=parts))


class OllamaChat:
    def __init__(self, model, system_prompt, tools, think=True, max_output_tokens=4096, num_ctx=32768):
        self.model = model
        self.client = ollama.Client()  # the local Ollama server, on localhost:11434
        self.tools = [{"type": "function", "function": tool} for tool in tools]
        self.messages = [{"role": "system", "content": system_prompt}]
        self.think = think
        self.options = {"num_ctx": num_ctx, "num_predict": max_output_tokens}

    def add_user_message(self, message):
        self.messages.append({"role": "user", "content": message})

    def generate(self):
        response = self.client.chat(
            model=self.model,
            messages=self.messages,
            tools=self.tools,
            think=self.think,
            options=self.options,
        )
        message = response.message
        self.messages.append(message)

        return Turn(
            tool_calls=[ToolCall(call.function.name, dict(call.function.arguments)) for call in message.tool_calls or []],
            thoughts=message.thinking or None,
            input_tokens=response.prompt_eval_count or 0,
            output_tokens=response.eval_count or 0,
            truncated=response.done_reason == "length",
        )

    def add_tool_results(self, results):
        for call, result in results:
            self.messages.append({"role": "tool", "tool_name": call.name, "content": json.dumps(result, default=str)})
