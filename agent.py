import asyncio
import json
import time
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from dataclasses import replace
from pydantic_ai import (Agent, BinaryContent, ModelAPIError, ModelHTTPError, ModelRequest, ModelRetry,
                         RetryPromptPart, TextOutput, Tool, ToolOutput, ToolReturnPart, UnexpectedModelBehavior,
                         UsageLimitExceeded, UsageLimits, UserPromptPart, capture_run_messages)
from pydantic_ai.capabilities import ProcessHistory
from analyzers import ANALYZERS
from chat_backends import read_response
from common import (CONFIG, make_parser, model_and_settings, model_name, parse_args, report_fields, report_path,
                    skill_files, skill_instructions, skills_prompt, trace_to_logfire)

class SkillTools:
    def __init__(self, skills):
        self.skills = skills
        self.loaded = []  # in the order the model first loaded them
        self.files_read = []  # as skill/path

    def no_skill(self, name):
        return {"error": f"there is no skill named {name!r}; the skills are {', '.join(self.skills)}"}

    def load_skill(self, name: str):
        """Loads a skill's instructions, which say how to judge what the skill is for, with the files that come with
        it, like worked examples, which read_skill_file reads. A skill with no files has no files field.

        Args:
            name: The skill's name, exactly as listed under Skills.
        """
        skill = self.skills.get(name)
        if skill is None:
            return self.no_skill(name)
        if name not in self.loaded:
            self.loaded.append(name)
        files = skill_files(skill)
        return {"name": name, "instructions": skill_instructions(skill), **({"files": files} if files else {})}

    def read_skill_file(self, skill: str, path: str):
        """Reads one of the files that come with a skill, like its worked examples.

        Args:
            skill: The skill's name.
            path: The file's path, exactly as load_skill listed it.
        """
        found = self.skills.get(skill)
        if found is None:
            return self.no_skill(skill)
        files = skill_files(found)
        if path not in files:
            return {"error": f"the {skill} skill has no file {path!r}; its files are {', '.join(files) or 'none'}"}
        if f"{skill}/{path}" not in self.files_read:
            self.files_read.append(f"{skill}/{path}")
        return {"skill": skill, "path": path, "content": (found.path.parent / path).read_text()}

    def tools(self):
        return [Tool(self.load_skill), Tool(self.read_skill_file)]

def printable(value):
    # an image a tool returns, like a screenshot, by its size rather than its bytes
    if isinstance(value, BinaryContent):
        return f"<{value.media_type}, {len(value.data)} bytes>"
    return str(value)

def cut_off(error):
    # Ollama answers a tool call cut off at the output cap with an error, not with the part of it the model wrote:
    # HTTP 500, "invalid tool call arguments … unexpected end of JSON input"
    text = f"{error} {getattr(error, 'body', '')}".lower()
    return (isinstance(error, ModelHTTPError) and error.status_code == 500
            and ("unexpected end of json input" in text or "invalid tool call arguments" in text))

def run_agent(*args, **kwargs):
    return asyncio.run(run_agent_async(*args, **kwargs))

async def run_agent_async(args, system_prompt, user_prompt, tools, submission, review, reject, toolsets=(),
                          session=None, request_limit=CONFIG.agent.max_requests, process_history=None,
                          output_name="submit_report", text_submission=False, cut_off_message=None,
                          read_reply=None, stuck=None):
    model, settings = model_and_settings(args)
    name = model_name(args)
    limits = CONFIG.agent
    output_retries = limits.max_text_replies + limits.max_rejections + limits.max_invalid_reports
    usage = Counter()

    def from_text(text: str):
        recorded = read_reply(text) if read_reply else None
        if recorded is not None:
            told, done = recorded
            usage["replies_recorded"] += 1
            print(f"=====Recorded the answers in {name}'s reply=====")
            if not done:
                raise ModelRetry(told)
            print(f"{told}\n")
        if not submission.model_fields:
            submitted = submission()
        else:
            start, end = text.find("{"), text.rfind("}")
            try:
                submitted = submission.model_validate(json.loads(text[start:end + 1]) if 0 <= start < end else None)
            except ValueError:  # not JSON, or not in the submission's shape
                raise ModelRetry(f"Hand in your answers by calling {output_name}.")
        usage["text_submissions"] += 1
        print(f"=====Took the submission from {name}'s reply=====")
        return submitted

    output_type = ToolOutput(submission, name=output_name)
    agent = Agent(model, instructions=system_prompt, tools=tools, toolsets=toolsets, model_settings=settings,
                  output_type=[output_type, TextOutput(from_text)] if text_submission else output_type,
                  retries={"tools": limits.max_tool_retries, "output": output_retries},
                  capabilities=[ProcessHistory(process_history)] if process_history else None)

    @agent.output_validator
    def check_report(report):
        unverified = review(report)
        if not unverified or usage["rejected_submissions"] >= limits.max_rejections:
            return report
        usage.update(rejected_submissions=1, unbacked_claims=len(unverified))
        print(f"=====Report rejected: {len(unverified)} unbacked item(s)=====")
        raise ModelRetry(reject(unverified))

    def show_turn(turn):
        if turn.thoughts:
            print(f"\n====={name}'s thoughts=====\n{turn.thoughts}\n")
        if turn.text:
            print(f"\n====={name} says=====\n{turn.text}\n")

        usage.update(llm_requests=1, input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
                     truncated_replies=int(turn.truncated))

        if turn.truncated:
            print(f"====={name} was cut off at the output token limit=====")

        if not turn.tool_calls and text_submission:
            usage["text_replies"] += 1  # read as a submission, and sent back if it isn't one
        elif not turn.tool_calls:
            if usage["text_replies"] >= limits.max_text_replies:
                print(f"====={name} has no more function calls=====")
                return False
            usage["text_replies"] += 1
            print(f"=====No tool call, asking {name} to call a tool=====")
        for tool_name, args in zip(turn.tool_calls, turn.tool_args):
            # the arguments as the model wrote them, before validation drops a field it doesn't know
            print(f"====={name} wants to use the {tool_name} tool=====\n{args}")
        return True

    def show_request(request):
        # prints what goes back to the model: tool results, and what it is asked to fix
        for part in request.parts:
            if isinstance(part, ToolReturnPart):
                result = json.dumps(part.content, indent=2, ensure_ascii=False, default=printable)
                print(f"=====Result of {part.tool_name}=====\n{result}\n")
            elif isinstance(part, RetryPromptPart):
                print(f"=====Sent back to {name}=====\n{part.model_response()}\n")

    current = {}  # the attempt running now, whose usage a continuation carries on

    async def attempt(prompt, history, run_usage):
        with agent.parallel_tool_call_execution_mode("sequential"):
            usage_limits = UsageLimits(request_limit=request_limit)
            async with agent.iter(prompt, message_history=history, usage=run_usage,
                                  usage_limits=usage_limits) as agent_run:
                current["run"] = agent_run
                last = time.perf_counter()
                resumed = history is not None  # its first request was shown before it was cut off
                async for node in agent_run:
                    # the time since the last node went to running it: the model's reply, or the tools it called
                    now = time.perf_counter()
                    elapsed, last = now - last, now
                    if Agent.is_call_tools_node(node):
                        # the model has replied, and its tool calls haven't run yet
                        usage["model_s"] += elapsed
                        if not show_turn(read_response(node.model_response)):
                            return None
                    elif Agent.is_model_request_node(node):
                        usage["tools_s"] += elapsed
                        if not resumed:
                            show_request(node.request)
                        resumed = False
                        why = stuck() if stuck else None
                        if why:
                            print(f"=====The run stopped: {why}=====")
                            return None
                return agent_run.result.output

    async def run():
        # a reply cut off at the output cap is answered by going on with the same conversation, told why: sent again as
        # it was, the request would only be cut off again. Ollama doesn't return the part it wrote, so nothing in it
        # was taken, and the tools' results and the answers recorded before it are kept
        prompt, history, run_usage = user_prompt, None, None
        async with session or nullcontext():
            while True:
                with capture_run_messages() as messages:
                    try:
                        return await attempt(prompt, history, run_usage)
                    except ModelAPIError as error:
                        if (cut_off_message is None or not cut_off(error)
                                or usage["cut_off_retries"] >= limits.max_cut_off_retries
                                or not messages or not isinstance(messages[-1], ModelRequest)):
                            raise
                usage.update(cut_off_retries=1, truncated_replies=1)
                print(f"====={name} was cut off at the output token limit in the middle of a tool call, so it is "
                      f"asked to go on in smaller calls ({usage['cut_off_retries']} of "
                      f"{limits.max_cut_off_retries})=====\n{cut_off_message}\n")
                parts = [part for part in messages[-1].parts
                         if not (isinstance(part, UserPromptPart) and part.content == cut_off_message)]
                history = [*messages[:-1], replace(messages[-1], parts=[*parts, UserPromptPart(cut_off_message)])]
                prompt, run_usage = None, current["run"].usage

    try:
        submitted = await run()
    except (UnexpectedModelBehavior, UsageLimitExceeded, ModelAPIError) as e:
        print(f"=====The run stopped: {e}=====")
        submitted = None

    if submitted is None:
        print("=====No valid report was submitted=====")
    else:
        print(f"====={name} has submitted the final report=====")
    return submitted, usage

def main():
    trace_to_logfire()
    args = parse_args(make_parser("Find problems on a web page with an agent that calls the tools itself and hands "
                                  "in one report.", ANALYZERS))
    analyzer = ANALYZERS[args.analyzer](args)
    skills = SkillTools(analyzer.skills)
    system_prompt = f"{analyzer.system_prompt()}\n\n# Skills\n\n{skills_prompt(analyzer.skills)}"

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    submitted, usage = run_agent(args, system_prompt, analyzer.user_prompt(), analyzer.tools() + skills.tools(),
                                 analyzer.submission, analyzer.review, analyzer.reject, analyzer.toolsets(),
                                 analyzer.session(), analyzer.request_limit())
    analyzer.finish()

    if submitted is None:
        return

    findings, fields = analyzer.agent_report(submitted)
    report = analyzer.agent_report_type(
        **report_fields(args, started_at, start, usage),
        counts=dict(Counter(f.severity for f in findings)),
        narrative=submitted.narrative,
        rejected_submissions=usage["rejected_submissions"],
        unbacked_claims=usage["unbacked_claims"],
        text_replies=usage["text_replies"],
        skills_loaded=skills.loaded,
        skill_files_read=skills.files_read,
        findings=findings,
        **fields,
    )
    path = report_path(analyzer.name, args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
