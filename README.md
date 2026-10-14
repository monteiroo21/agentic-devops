# Exploratory Demo

Two analyzers that check a web page by pairing a static tool with a language model. The static tool finds
what it can measure. The model then looks for the problems the tool can't see. Both analyzers can run on
Google's Gemini API or on a local model through Ollama.

## What the analyzers do

### Link analyzer: `web_analyzer.py`

Finds broken links on a page.

1. **Static check.** The `check_page_links` tool finds every link and resource on the page (anchors, images,
   scripts, stylesheets) and requests each one. It records the status code, redirect chain, final URL and,
   for HTML pages, the title and first heading. Broken, unreachable and blocked links are reported from
   these results.
2. **Model.** A status of 200 doesn't mean the link works as intended, so the model looks for problems the
   status code doesn't show:
   - soft 404s: the server returns 200, but the page is an error page
   - redirects that end on the homepage or on a login page
   - link text that doesn't match the page the link leads to

   The model reads pages with `fetch_page_text` before it reports a problem. If it reports something no
   tool result backs up, the report is sent back to it, at most twice.

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
  model about one criterion and a few elements at a time, with answers in JSON. This version is meant for
  small local models, which struggle to drive a long tool-calling loop.

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

2. Start the server, unless it's already running as a service (`ollama list` tells you):

   ```bash
   ollama serve
   ```

3. Download the model:

   ```bash
   ollama pull qwen3:4b-instruct
   ```

The analyzers connect to Ollama at `http://localhost:11434`, with a 32,768-token context window.
`qwen3:4b-instruct` has no thinking mode, so run it with `--no-think`.

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

### Link analyzer

```bash
uv run web_analyzer.py                                 # Gemini
uv run web_analyzer.py --backend ollama --no-think     # qwen3:4b-instruct on Ollama
```

The script asks for a message. Type the page to check, for example:

```
Enter your message: Check the links on http://www.veloribeira.pt/
```

### Accessibility analyzer

The accessibility analyzers take the URL as an argument:

```bash
# agent
uv run accessibility_analyzer.py http://www.veloribeira.pt/
uv run accessibility_analyzer.py http://www.veloribeira.pt/ --backend ollama --no-think

# workflow
uv run accessibility_workflow.py http://www.veloribeira.pt/
uv run accessibility_workflow.py http://www.veloribeira.pt/ --backend ollama --no-think --batch-size 4
```

### Flags

| Flag                      | Accessibility agent | Accessibility workflow | Description                                                                                        |
| ------------------------- | :-----------------: | :--------------------: | -------------------------------------------------------------------------------------------------- |
| `url`                     |          ✓          |           ✓            | The page to check. Must start with `http://` or `https://`.                                        |
| `--backend {gemini,ollama}` |          ✓          |           ✓            | Use Google's API or a local model. Default `gemini`.                                               |
| `--model NAME`            |          ✓          |           ✓            | The model to use. Default `gemini-3.1-flash-lite` for Gemini, `qwen3:4b-instruct` for Ollama.      |
| `--no-think`              |          ✓          |           ✓            | Ollama only. Answer without thinking first, which is much faster on a CPU.                         |
| `--max-output-tokens N`   |          ✓          |           ✓            | Ollama only. The most tokens the model may write per turn, thinking included. Default `4096`.      |
| `--output PATH`           |          ✓          |           ✓            | Where to save axe's raw results. Default `reports/axe_raw.json`.                                   |
| `--batch-size N`          |                     |           ✓            | How many elements the model judges in one call. Default `8`.                                       |

`--no-think` and `--max-output-tokens` are rejected with `--backend gemini`. Run any script with `--help` to
see its flags.

## Project layout

```
web_analyzer.py            link analyzer
accessibility_analyzer.py  accessibility analyzer (agent)
accessibility_workflow.py  accessibility analyzer (workflow)
chat_backends.py           Gemini and Ollama clients behind one interface
testsite/                  test server and its pages
reports/                   generated reports
```
