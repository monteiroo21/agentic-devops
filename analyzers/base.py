from contextlib import nullcontext
from common import CONFIG

class Analyzer:
    # One analysis of one page. agent.py and workflow.py can run any analyzer; a subclass says what is specific to it
    name = None  # in the command line and in the report names
    description = None  # for --help
    skills = None
    submission = None  # what the agent hands in with submit_report
    agent_report_type = None
    workflow_report_type = None
    judge_prompt = None  # the workflow model's system prompt

    def __init__(self, args):
        self.args = args

    @staticmethod
    def add_arguments(parser):
        pass  # flags only this analyzer has

    @staticmethod
    def add_agent_arguments(parser):
        pass  # flags only this analyzer's agent has

    def finish(self):
        pass  # once the tools are done, e.g. to save their raw results

    # Agent

    def system_prompt(self):
        raise NotImplementedError  # what to do and how; the skills are added after it

    def user_prompt(self):
        raise NotImplementedError

    def tools(self):
        raise NotImplementedError  # its tools, which record what they return

    def toolsets(self):
        return []  # tools that come in a set, like a browser's

    def session(self):
        return nullcontext()  # what the agent's run happens inside, like the browser its tools share

    def request_limit(self):
        return CONFIG.agent.max_requests

    def review(self, report):
        raise NotImplementedError  # the submitted items no tool result backs

    def reject(self, unverified):
        raise NotImplementedError  # the message that sends them back

    def agent_report(self, submitted):
        raise NotImplementedError  # the findings, and the report fields only this analyzer has

    # Workflow

    def collect(self):
        raise NotImplementedError  # runs the tools; False if there is nothing to judge

    def instructions(self, skill_name, ids):
        raise NotImplementedError  # what a question's ids are, then how the skill says to judge them

    def ask_questions(self, questions):
        raise NotImplementedError  # asks each question with questions.ask()

    def workflow_report(self, questions):
        raise NotImplementedError  # the findings, the verdicts, and the report fields only this analyzer has
