# Exploratory Demo

Two analyzers that check a web page by pairing a static tool with a language model. The static tool finds
what it can measure. The model then looks for the problems the tool can't see. Both analyzers can run on
Google's Gemini API or on a local model through Ollama, and talk to the model through
[Pydantic AI](https://ai.pydantic.dev/).

Each analyzer comes in two versions, run by two scripts:

- **`agent.py` (agent).** The model calls the tools itself, in a loop, and submits one report. Every item it
  reports must be backed by a tool result: if one isn't, the report is sent back to it, at most twice, and
  anything still unbacked is kept out of the findings.
- **`workflow.py` (workflow).** The script runs the tools itself, then asks the model one fixed question at a
  time about a few elements, with answers in JSON. The model only judges; it never chooses what to do.

The workflow is meant for small local models, which struggle to drive a long tool-calling loop. Running both
on the same page compares the two approaches.

### Skills

How to judge each kind of problem is written down as a skill in `skills/<analyzer>/<name>/SKILL.md`, with
worked examples in `references/`. Both versions use the same skills, in different ways:

- The agent only sees each skill's name and description at first. It loads a skill's instructions with
  `load_skill` when it needs them, and reads the examples with `read_skill_file` when a case is hard to
  decide.
- The workflow puts the whole skill, examples included, into every question about it.

The system prompts are in `prompts/<analyzer>/`: `agent.md` for the agent and `judge.md` for the workflow's
model.

## What the analyzers do

### Link analyzer: `links`

Finds broken links on a page.

1. **Static check.** The `check_page_links` tool finds every link and resource on the page (anchors, images,
   scripts, stylesheets) and requests each one, over plain HTTP. It records the status code, redirect chain,
   final URL and, for HTML pages, the title and first heading. From these alone, the code marks each link
   broken, unreachable, blocked, redirected or ok. Broken and unreachable links are reported as errors
   without the model.
2. **Model.** The model explains each broken link and suggests a fix. A status of 200 doesn't mean the link
   works as intended, so the model also looks for problems the status code doesn't show:
   - soft 404s: the server returns 200, but the page is an error page
   - redirects that end on the homepage or on a login page
   - link text that doesn't match the page the link leads to

The model is never shown the code's verdict on a link, so it judges each one from the facts.

- **Agent.** The model checks the links with `check_page_links`, and reads the pages it can't judge from
  their title and heading with `fetch_page_text`. A soft 404, a text mismatch or any other problem that
  depends on a page's content is only accepted if the model read that page.
- **Workflow.** The script checks the links and reads every page the working links lead to itself. It then
  asks the model, in this order:
  1. notes on the broken links
  2. soft-404, for each page the links lead to
  3. redirect, for each redirected link
  4. text-mismatch, for each link with text

  A link that fails one question is left out of the later ones, so it gets at most one finding from the
  model.

[LinkChecker](https://linkchecker.github.io/linkchecker/) is the static baseline the analyzer is compared
against.

### Accessibility analyzer: `accessibility`

Finds accessibility problems against WCAG 2.2 levels A and AA.

1. **Static check.** [axe-core](https://github.com/dequelabs/axe-core) runs on the page in headless Chromium
   (through Playwright). Its violations are confirmed problems. Its "incomplete" results are elements axe
   couldn't decide, and they need review.
2. **Model.** The model explains each violation and suggests a fix. It also decides on the elements axe left
   undecided (pass, fail or manual review). Then it reads the page as a user would and judges what axe
   can't, because axe only checks that things exist, not that they make sense:
   - alt text (1.1.1): does it describe what the image adds?
   - link purpose (2.4.4): can a user tell where each link goes?
   - page title (2.4.2): does it say what the page is about?
   - page language (3.1.1, 3.1.2): does `lang` match the text?
   - headings (1.3.1, 2.4.6): do they describe their sections?

What the model reads comes from `get_page_content`, a script that runs in the page and returns its title,
language, text, headings, images, links and the passages with a `lang` of their own.

- **Agent.** The model runs `run_axe` and `get_page_content` itself, and refers to elements by the selectors
  they return. Every element it reports must be one those tools returned.
- **Workflow.** The script runs axe and reads the page itself. It then asks the model about one criterion and
  a few elements at a time. The model sees elements by short ids, like `img-3`, rather than by selector, and
  the script maps each answer back to its element.

#### The agent's browser

With Gemini, the accessibility agent also gets a browser, from the
[Pydantic AI harness](https://pydantic.dev/docs/ai/harness/playwright/), to reach what only appears after an
interaction: menus, dialogs and the errors a form shows when it is sent empty. It can look at the page
(`snapshot`, `screenshot`, `wait_for`) and use it as a visitor would (`click`, `hover`, `press_key`,
`type_text`, `select_option`, `go_back`).

- `run_axe` and `get_page_content` share the browser's page. Given a URL, they load it; without one, they
  check the page as the agent left it.
- Each check after the agent changed the page starts a new state (`s0` is the page as loaded, then `s1`,
  `s2`, …), recorded with the actions that led to it. Every review and finding names its state, and is
  checked against that state's results.
- The browser can only go to the host of the page being checked, and never sends anything: requests other
  than GET are blocked, and form submissions are cancelled once the page's own validation has run.
- For text over an image whose contrast axe couldn't decide, the agent takes a screenshot and passes or fails
  it only when the answer is clear.

`--no-browser` turns the browser off, to compare runs with and without it. Ollama models never get it.

## Models

| Backend            | Model                   | Runs on                   |
| ------------------ | ----------------------- | ------------------------- |
| `gemini` (default) | `gemini-3.5-flash-lite` | Google's Gemini API       |
| `ollama`           | `qwen3:4b-instruct`     | your machine, with Ollama |

These are the defaults for each backend, set in `config.yaml`. Use `--model` to pick another model.

## Setup

You need Python 3.13, [uv](https://docs.astral.sh/uv/) and Node.js with npm. Docker is only needed for local
models, to run Ollama.

```bash
uv sync                             # Python dependencies, including the Pydantic AI harness
npm install                         # axe-core, injected into the page by Playwright
uv run playwright install chromium  # the headless browser for the accessibility analyzers
```

### Gemini API key

To use Gemini, create a `.env` file in the project root with your API key:

```
GEMINI_API_KEY=your-api-key
```

Runs are also traced to [Logfire](https://logfire.pydantic.dev/) when a Logfire token is set, for example with
`LOGFIRE_TOKEN` in the same file. Without one, nothing is sent.

### Ollama

Ollama runs in Docker, from `docker-compose.yml`. From the project root:

1. Start it:

   ```bash
   docker compose up -d
   ```

2. Download the model, the first time only:

   ```bash
   docker compose exec ollama ollama pull qwen3:4b-instruct
   ```

3. Stop it when you're done:

   ```bash
   docker compose down
   ```

The downloaded models are kept in the `ollama` volume, so they survive `docker compose down`.

The analyzers connect to Ollama's OpenAI-compatible API at `http://localhost:11434/v1`, the port the container
publishes. That API can't set the context window per request, so `docker-compose.yml` sets it for the whole
server, to 32,768 tokens (`OLLAMA_CONTEXT_LENGTH`). Without it, Ollama uses 4,096 tokens and cuts longer
prompts. `qwen3:4b-instruct` has no thinking mode, so run it with `--no-think`.

## Running the test server

`testsite/` is a fictional Porto bike shop, `www.veloribeira.pt`, with planted link and accessibility
problems. The same process also serves the fictional external sites the shop links to, and picks the site
from the `Host` header.

1. Add these lines to `/etc/hosts` (for example with `sudo nano /etc/hosts`):

   ```
   127.0.0.1  www.veloribeira.pt www.douroride.pt www.trailgear-supply.com routes.pedalmap.eu www.portobikefest.pt
   127.0.0.2  www.ciclovia-norte.pt
   ```

   Nothing listens on `127.0.0.2`, so `www.ciclovia-norte.pt` refuses connections like a dead server.

2. Allow port 80 without root, so the URLs don't carry a port. The setting lasts until the next reboot:

   ```bash
   sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
   ```

3. Start the server from the project root:

   ```bash
   uv run testsite/server.py
   ```

| Flag           | Default | Description                                                                                     |
| -------------- | ------- | ----------------------------------------------------------------------------------------------- |
| `--port PORT`  | `80`    | Port to listen on. On any other port, the links between the sites are rewritten to include it. |
| `--no-latency` | off     | Answer straight away instead of adding a realistic delay.                                       |

Each of these pages has its own planted problems:

- `http://www.veloribeira.pt/`
- `http://www.veloribeira.pt/partners`
- `http://www.veloribeira.pt/blog/douro-valley-3-day-itinerary`

`http://www.veloribeira.pt/login` has a form, for trying the agent's browser.

## Running the analyzers

Run every command from the project root, because the scripts use relative paths to `node_modules/` and
`reports/`. Both scripts take the analyzer, `links` or `accessibility`, then the page to check.

### Link analyzer

```bash
# agent
uv run agent.py links http://www.veloribeira.pt/
uv run agent.py links http://www.veloribeira.pt/ --backend ollama --no-think

# workflow
uv run workflow.py links http://www.veloribeira.pt/
uv run workflow.py links http://www.veloribeira.pt/ --backend ollama --no-think --batch-size 4
```

### Accessibility analyzer

```bash
# agent, with the browser
uv run agent.py accessibility http://www.veloribeira.pt/
uv run agent.py accessibility http://www.veloribeira.pt/ --no-browser
uv run agent.py accessibility http://www.veloribeira.pt/ --backend ollama --no-think

# workflow
uv run workflow.py accessibility http://www.veloribeira.pt/
uv run workflow.py accessibility http://www.veloribeira.pt/ --backend ollama --no-think --batch-size 4
```

### Flags

| Flag                        | Link agent | Link workflow | Accessibility agent | Accessibility workflow | Description                                                                                   |
| --------------------------- | :--------: | :-----------: | :-----------------: | :--------------------: | --------------------------------------------------------------------------------------------- |
| `url`                       |     ✓      |       ✓       |          ✓          |           ✓            | The page to check. Must start with `http://` or `https://`.                                   |
| `--backend {gemini,ollama}` |     ✓      |       ✓       |          ✓          |           ✓            | Use Google's API or a local model. Default `gemini`.                                          |
| `--model NAME`              |     ✓      |       ✓       |          ✓          |           ✓            | The model to use. Default `gemini-3.5-flash-lite` for Gemini, `qwen3:4b-instruct` for Ollama. |
| `--no-think`                |     ✓      |       ✓       |          ✓          |           ✓            | Ollama only. Answer without thinking first, which is much faster on a CPU.                    |
| `--max-output-tokens N`     |     ✓      |       ✓       |          ✓          |           ✓            | Ollama only. The most tokens the model may write per turn, thinking included. Default `4096`. |
| `--batch-size N`            |            |       ✓       |                     |           ✓            | How many elements the model judges in one call. Default `8`.                                  |
| `--no-browser`              |            |               |          ✓          |                        | Don't give the agent the browser. Ollama models never have it.                                |
| `--output PATH`             |            |               |          ✓          |           ✓            | Where to save axe's raw results. Default `reports/axe_raw.json`.                              |

`--no-think` and `--max-output-tokens` are rejected with `--backend gemini`. Run any command with `--help` to
see its flags, for example `uv run agent.py accessibility --help`.

### Settings

`config.yaml` holds the rest of the settings, checked when a script starts:

- `models`, `gemini`, `ollama`: the default models, Gemini's thinking level, and Ollama's address and output
  limit
- `agent`: how often the agent's report may be sent back, how many retries it gets, and the most model calls
  in a run (50)
- `workflow`: the batch size, and how often the model is asked again about elements it left out
- `browser`: the agent's browser: how long to wait for animations before a check, whether it may reach
  addresses on this machine (needed for the test site), how much page text its tools return, and the most
  model calls in a run that has it (80)
- `accessibility`, `links`: where the skills and prompts are, and how much of each page the model is shown

### Reports

Each run saves a JSON report to `reports/`, named after the analyzer, the model, the host and path of the page,
and the time of the run:

```
reports/links_gemini-3.5-flash-lite_www-veloribeira-pt_20261021-143000.json
reports/links-workflow_qwen3-4b-instruct_no-think_www-veloribeira-pt_partners_20261021-143000.json
reports/accessibility_…   reports/accessibility-workflow_…
```

Every report starts with the same fields:
- `mode`: `agent` or `workflow`
- `target`, `model`, `thinking` and `started_at`
- `duration_s`, `llm_requests`, `input_tokens`, `output_tokens` and `truncated_replies`
- `counts`: the findings per severity

Then each report has its `findings`, each with a severity:
- `error`: a broken or unreachable link, or an axe violation
- `warning`: a problem the model found
- `needs-review`: something left undecided
- `info`: a blocked or redirected link, or an element axe couldn't decide that the model passed

Agent reports also have:
- the model's `narrative`
- how often its report was sent back (`rejected_submissions`, `unbacked_claims`), and the items still
  unbacked after the last time (`unverified`)
- the skills it loaded and the skill files it read
- for links, every claim that was sent back and what became of it (`rejected_claims`)
- for accessibility, whether it had the browser (`browser`) and every state of the page it checked
  (`states`), with each finding naming its state

Workflow reports also have per-criterion `criteria` counts and a `verdicts` list with every element judged,
passes included.

## Project layout

```
agent.py              runs an analyzer as an agent: the model calls the tools and submits one report
workflow.py           runs an analyzer as a workflow: the script runs the tools and asks the model questions
common.py             settings, command line, skills and report fields shared by both
chat_backends.py      Gemini and Ollama models behind one interface
config.yaml           settings
analyzers/            what is specific to each analyzer: its tools, questions, evidence checks and reports
tools/                link checking, axe, page content and the agent's browser
skills/               how to judge each kind of problem, with worked examples
prompts/              the system prompts
testsite/             test server and its pages
docker-compose.yml    Ollama, for local models
reports/              generated reports
```
