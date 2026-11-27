# Agentic QE for websites

This project checks websites for quality problems with a **fleet of agents**. A cloud LLM only coordinates the
work. Small local models (SLMs) do the judging, and each one has its own skills and tools. Two quality domains are
covered so far: **links** and **accessibility** (WCAG 2.2, levels A and AA).

The idea is that deterministic tools find what they can measure, and the models only judge what those tools can't
see. For example, a link that returns 200 but leads to an error page, or alt text that doesn't describe its image.

## How it works

```
your request ──► coordinator  (Gemini, cloud: plans only, never judges)
                  │
                  ├─ crawl_site        → the site's pages
                  ├─ run_static_check  → link checker, axe-core      (facts, no model)
                  └─ delegate          → one agent on one page
                                │
                agents  (qwen3:4b-instruct on Ollama, local CPU)
                  link-agent    soft-404 · redirect · text-mismatch · link-purpose
                  page-agent    heading · page-title · page-language
                  image-agent   alt-text
                  axe-review    elements axe-core couldn't decide
                                │
                report  →  reports/fleet_*.json  +  .html
```

- **Coordinator** (`fleet.py`, prompt in `agents/coordinator.md`). It reads a request in plain language, crawls the
  site, and decides which static checks and agents to run on which pages. It doesn't judge anything: the static
  checks' results and the agents' findings go into the report as they are. Before it can submit the report, every
  page has to be checked or left out, with a reason.
- **Agents** (`agents/<name>.md`). Each agent has a short definition: its skills, its tools and a step limit. Its
  skills live in `skills/<agent>/<skill>/SKILL.md`, with worked examples. Its tools, in `toolkits.py`, show it only
  the part of the page it needs. The agent answers every item with a reason, then a verdict (pass, fail or unsure),
  and, for a fail, an impact and a suggested fix. The code checks the answers and sends back any that are invalid
  or missing.
- **Static checks** (`tools/`). The link checker requests every link and resource on a page, and axe-core runs in
  headless Chromium. Their results are taken as facts.

The agents can also run on Gemini (`--worker-backend gemini`), to compare an SLM fleet with a cloud LLM on the same
work.

### Earlier stages

These still run, and are used for comparison:

1. `agent.py` / `workflow.py`: one analyzer (links or accessibility) on one page. They compare a model that drives
   its own tool loop (agent) with a script that asks the model fixed questions (workflow).
2. `orchestrator.py`: a whole site. A Gemini orchestrator sends batched questions to qwen workers, then reviews
   their verdicts in rounds.
3. `fleet.py`: the current design. A thin orchestrator coordinates specialised SLM agents, each with its own skills
   and tools.

### Where it's heading

`diagrams/architecture.png` and `diagrams/flow-diagram.png` show the target design, most of which isn't built yet:
- runs triggered from a CI/CD pipeline
- a LangGraph executor with checkpoints
- more quality domains: static analysis, vulnerability detection and test generation, the last run in a sandbox
- quality gates that decide pass or fail
- tracing and offline evaluation with Langfuse and DeepEval

## Test site

`testsite/` is a fictional Porto bike shop, `www.veloribeira.pt`, with link and accessibility problems planted in it.
The same server also plays the external sites the shop links to. To set up `/etc/hosts` and port 80, see
[testsite/README.md](testsite/README.md).

## Setup

You need Python 3.13, [uv](https://docs.astral.sh/uv/), Node.js with npm, and Docker (for Ollama).

```bash
uv sync
npm install                                  # axe-core
uv run playwright install chromium
echo "GEMINI_API_KEY=your-api-key" > .env

docker compose up -d                         # Ollama, with a 32k context window
docker compose exec ollama ollama pull qwen3:4b-instruct
```

The settings (models, limits, and where the prompts and skills are) are in `config.yaml`.

## Running

Run these from the project root:

```bash
uv run testsite/server.py                                    # the test site

uv run fleet.py http://www.veloribeira.pt/ --no-think        # the whole fleet on the whole site
uv run fleet.py http://www.veloribeira.pt/ --no-think --request "Check only the links of the blog"
uv run fleet.py http://www.veloribeira.pt/ --worker-backend gemini

uv run specialists.py link http://www.veloribeira.pt/ --skills soft-404 --no-think   # one agent, one page
uv run crawl.py http://www.veloribeira.pt/                   # only the crawl
```

Crawls are saved in `crawls/` and reused for 24 hours (`--recrawl` forces a new crawl). Reports are saved in
`reports/`. Run any script with `--help` to see its flags.

## Layout

```
fleet.py           the coordinator: crawl, static checks, delegation, report
specialists.py     runs the fleet's agents, and checks and records their answers
toolkits.py        each agent's tools
agents/            the coordinator's prompt and each agent's definition
skills/            how to judge each kind of problem, with examples
crawl.py           the site crawler
tools/             link checker, axe-core, page content, browser
report_page.py     the HTML report
orchestrator.py    earlier stage: orchestrator and workers
agent.py, workflow.py, analyzers/, prompts/   earliest stage: single-page analyzers
testsite/          the test site
diagrams/          architecture and flow diagrams (target design)
presentations/     sprint presentations
```
