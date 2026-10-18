import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pydantic_ai import (Agent, ModelRetry, RetryPromptPart, ToolOutput, ToolReturnPart, UnexpectedModelBehavior,
                         UsageLimitExceeded, UsageLimits)
from analyzers import ANALYZERS
from chat_backends import read_response
from common import (CONFIG, make_parser, model_and_settings, model_name, parse_args, report_fields, report_path,
                    skills_prompt)

# The model calls the tools itself and submits one report

def run_agent(args, system_prompt, user_prompt, tools, submission, review, reject):
    model, settings = model_and_settings(args)
    name = model_name(args)
    limits = CONFIG.agent
    output_retries = limits.max_text_replies + limits.max_rejections + limits.max_invalid_reports
    agent = Agent(model, instructions=system_prompt, tools=tools, model_settings=settings,
                  output_type=ToolOutput(submission, name="submit_report"),
                  retries={"tools": limits.max_tool_retries, "output": output_retries})
    usage = Counter()

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

        if not turn.tool_calls:
            if usage["text_replies"] >= limits.max_text_replies:
                print(f"====={name} has no more function calls=====")
                return False
            usage["text_replies"] += 1
            print(f"=====No tool call, asking {name} to call a tool=====")
        for tool_name in turn.tool_calls:
            print(f"====={name} wants to use the {tool_name} tool=====")
        return True

    def show_request(request):
        # prints what goes back to the model: tool results, and what it is asked to fix
        for part in request.parts:
            if isinstance(part, ToolReturnPart):
                result = json.dumps(part.content, indent=2, ensure_ascii=False, default=str)
                print(f"=====Result of {part.tool_name}=====\n{result}\n")
            elif isinstance(part, RetryPromptPart):
                print(f"=====Sent back to {name}=====\n{part.model_response()}\n")

    async def run():
        # Tools run one at a time, in the order the model called them
        with agent.parallel_tool_call_execution_mode("sequential"):
            usage_limits = UsageLimits(request_limit=limits.max_requests)
            async with agent.iter(user_prompt, usage_limits=usage_limits) as agent_run:
                async for node in agent_run:
                    if Agent.is_call_tools_node(node):
                        # the model has replied, and its tool calls haven't run yet
                        if not show_turn(read_response(node.model_response)):
                            return None
                    elif Agent.is_model_request_node(node):
                        show_request(node.request)
                return agent_run.result.output

    try:
        submitted = asyncio.run(run())
    except (UnexpectedModelBehavior, UsageLimitExceeded) as e:
        # out of retries, a reply cut off in the middle of a tool call, or the request limit
        print(f"=====The run stopped: {e}=====")
        submitted = None

    if submitted is None:
        print("=====No valid report was submitted=====")
    else:
        print(f"====={name} has submitted the final report=====")
    return submitted, usage

def main():
    args = parse_args(make_parser("Find problems on a web page with an agent that calls the tools itself and hands "
                                  "in one report.", ANALYZERS))
    analyzer = ANALYZERS[args.analyzer](args)
    system_prompt = f"{analyzer.system_prompt()}\n\n# Skills\n\n{skills_prompt(analyzer.skills)}"

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    submitted, usage = run_agent(args, system_prompt, analyzer.user_prompt(), analyzer.tools(), analyzer.submission,
                                 analyzer.review, analyzer.reject)
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
        findings=findings,
        **fields,
    )
    path = report_path(analyzer.name, args)
    path.write_text(report.model_dump_json(indent=2))
    print(f"=====Report saved to {path}=====")

if __name__ == "__main__":
    main()
