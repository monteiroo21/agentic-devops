# Exploratory Demo

Two analyzers that check a web page by pairing a static tool with a language model. The static tool finds
what it can measure. The model then looks for the problems the tool can't see. Both analyzers can run on
Google's Gemini API or on a local model through Ollama, and talk to the model through
[Pydantic AI](https://ai.pydantic.dev/).

## What the analyzers do

### Link analyzer: `link_analyzer.py` and `link_workflow.py`

Finds broken links on a page.

1. **Static check.** The `check_page_links` tool finds every link and resource on the page (anchors, images,
   scripts, stylesheets) and requests each one. It records the status code, redirect chain, final URL and,
   for HTML pages, the title and first heading. Broken, unreachable and blocked links are reported from
   these results.
2. **Model.** The model explains each broken link and suggests a fix. A status of 200 doesn't mean the link
   works as intended, so the model also looks for problems the status code doesn't show:
   - soft 404s: the server returns 200, but the page is an error page
   - redirects that end on the homepage or on a login page
   - link text that doesn't match the page the link leads to

There are two versions:

- **`link_analyzer.py` (agent).** The model calls the tools itself and submits one report. It reads pages
  with `fetch_page_text` before it reports a problem. If it reports something no tool result backs up, the
  report is sent back to it, at most twice.
- **`link_workflow.py` (workflow).** The script checks the links and reads every page they lead to itself.
  It then asks the model one question at a time about a few links, with answers in JSON:
  1. notes on the broken links
  2. soft-404, for each page the links lead to
  3. redirect, for each redirected link
  4. text-mismatch, for each link with text

  A link that fails one question is left out of the later ones, so it gets at most one finding from the
  model.

[LinkChecker](https://linkchecker.github.io/linkchecker/) is the static baseline the analyzer is compared
against.

### Accessibility analyzer: `accessibility_analyzer.py` and `accessibility_workflow.py`

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

There are two versions:

- **`accessibility_analyzer.py` (agent).** The model calls the tools itself and submits one report. Findings
  the tools don't back up are sent back, as in the link analyzer.
- **`accessibility_workflow.py` (workflow).** The script runs axe and reads the page itself. It then asks the
  model about one criterion and a few elements at a time, with answers in JSON.

The workflow versions are meant for small local models, which struggle to drive a long tool-calling loop.
Both versions of each analyzer share their command-line flags, agent loop and report format through
`common.py`. `chat_backends.py` sets up the Gemini and Ollama models.

## Models

| Backend            | Model                   | Runs on                   |
| ------------------ | ----------------------- | ------------------------- |
| `gemini` (default) | `gemini-3.1-flash-lite` | Google's Gemini API       |
| `ollama`           | `qwen3:4b-instruct`     | your machine, with Ollama |

These are the defaults for each backend. Use `--model` to pick another model.

## Setup

You need Python 3.13, [uv](https://docs.astral.sh/uv/) and Node.js with npm. Ollama is only needed for local
models.

```bash
uv sync                             # Python dependencies
npm install                         # axe-core, injected into the page by Playwright
uv run playwright install chromium  # the headless browser for the accessibility analyzers
```

### Gemini API key

To use Gemini, create a `.env` file in the project root with your API key:

```
GEMINI_API_KEY=your-api-key
```

### Ollama

1. Install Ollama. On Linux:

   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```

   For other systems, see [ollama.com/download](https://ollama.com/download).

2. Start the server with a 32,768-token context window, unless it's already running as a service
   (`ollama list` tells you):

   ```bash
   OLLAMA_CONTEXT_LENGTH=32768 ollama serve
   ```

   If it runs as a systemd service, set the context window there instead: run `sudo systemctl edit ollama`,
   add the lines below, and restart it with `sudo systemctl restart ollama`.

   ```
   [Service]
   Environment="OLLAMA_CONTEXT_LENGTH=32768"
   ```

3. Download the model:

   ```bash
   ollama pull qwen3:4b-instruct
   ```

The analyzers connect to Ollama's OpenAI-compatible API at `http://localhost:11434/v1`. That API can't set
the context window per request, which is why the server sets it. Without it, Ollama uses 4,096 tokens and
cuts longer prompts. `qwen3:4b-instruct` has no thinking mode, so run it with `--no-think`.

## Running the test server

`testsite/` is a fictional Porto bike shop, `www.veloribeira.pt`, with planted link problems. The same process
also serves the fictional external sites the shop links to, and picks the site from the `Host` header. It was
built for the link analyzer, and you can also run the accessibility analyzers on it.

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

## Running the analyzers

Run every command from the project root, because the scripts use relative paths to `node_modules/` and
`reports/`.

Every analyzer takes the page to check as its first argument.

### Link analyzer

```bash
# agent
uv run link_analyzer.py http://www.veloribeira.pt/
uv run link_analyzer.py http://www.veloribeira.pt/ --backend ollama --no-think

# workflow
uv run link_workflow.py http://www.veloribeira.pt/
uv run link_workflow.py http://www.veloribeira.pt/ --backend ollama --no-think --batch-size 4
```

### Accessibility analyzer

```bash
# agent
uv run accessibility_analyzer.py http://www.veloribeira.pt/
uv run accessibility_analyzer.py http://www.veloribeira.pt/ --backend ollama --no-think

# workflow
uv run accessibility_workflow.py http://www.veloribeira.pt/
uv run accessibility_workflow.py http://www.veloribeira.pt/ --backend ollama --no-think --batch-size 4
```

### Flags

| Flag                        | Link agent | Link workflow | Accessibility agent | Accessibility workflow | Description                                                                                   |
| --------------------------- | :--------: | :-----------: | :-----------------: | :--------------------: | --------------------------------------------------------------------------------------------- |
| `url`                       |     ✓      |       ✓       |          ✓          |           ✓            | The page to check. Must start with `http://` or `https://`.                                   |
| `--backend {gemini,ollama}` |     ✓      |       ✓       |          ✓          |           ✓            | Use Google's API or a local model. Default `gemini`.                                          |
| `--model NAME`              |     ✓      |       ✓       |          ✓          |           ✓            | The model to use. Default `gemini-3.1-flash-lite` for Gemini, `qwen3:4b-instruct` for Ollama. |
| `--no-think`                |     ✓      |       ✓       |          ✓          |           ✓            | Ollama only. Answer without thinking first, which is much faster on a CPU.                    |
| `--max-output-tokens N`     |     ✓      |       ✓       |          ✓          |           ✓            | Ollama only. The most tokens the model may write per turn, thinking included. Default `4096`. |
| `--batch-size N`            |            |       ✓       |                     |           ✓            | How many elements the model judges in one call. Default `8`.                                  |
| `--output PATH`             |            |               |          ✓          |           ✓            | Where to save axe's raw results. Default `reports/axe_raw.json`.                              |

`--no-think` and `--max-output-tokens` are rejected with `--backend gemini`. Run any script with `--help` to
see its flags.

### Reports

Each run saves a JSON report to `reports/`, named after the analyzer, the model, the host and path of the page,
and the time of the run:

```
reports/links_gemini-3.1-flash-lite_www-veloribeira-pt_20261015-143000.json
reports/links-workflow_qwen3-4b-instruct_no-think_www-veloribeira-pt_partners_20261015-143000.json
reports/accessibility_…   reports/accessibility-workflow_…
```

Every report starts with the same fields:
- `mode`: `agent` or `workflow`
- `target`, `model`, `thinking` and `started_at`
- `duration_s`, `llm_requests`, `input_tokens`, `output_tokens` and `truncated_replies`
- `counts`: the findings per severity

Agent reports then have the model's `narrative` and how often its report was sent back. Workflow reports have
per-criterion `criteria` counts and a `verdicts` list with every element judged, passes included.

## Project layout

```
link_analyzer.py           link analyzer (agent)
link_workflow.py           link analyzer (workflow)
accessibility_analyzer.py  accessibility analyzer (agent)
accessibility_workflow.py  accessibility analyzer (workflow)
common.py                  command line, agent loop, workflow questions and report fields shared by all four
chat_backends.py           Gemini and Ollama clients behind one interface
testsite/                  test server and its pages
reports/                   generated reports
```
