# Eastmoney A-Share Demo (Ollama + Web Console)

Run TradingAgents fully offline on a Mac with an A-share ticker:

- **LLM**: local [Ollama](https://ollama.com) `qwen3:8b` (no API key needed)
- **Data**: [Eastmoney](https://www.eastmoney.com) public endpoints (Yahoo Finance is geo-blocked for many CN networks)
- **UI**: web control panel with live node-level pipeline progress + a report viewer

## Files

| File | Purpose |
|---|---|
| `run_demo.py` | CLI entry: run one analysis (Moutai 600519.SS on 2026-08-21) |
| `web_serve.py` | FastAPI control panel + background pipeline runner (port 8790) |
| `web/index.html` | Single-file frontend, no CDN dependencies, works offline |
| `make_report_page.py` | Regenerates the report viewer from `reports/` |
| `reports/` | Sample output of the demo run (13 markdown reports + `index.html`) |

## Prerequisites

1. Create the venv and install TradingAgents from the repo root:

   ```bash
   cd <repo-root>
   python3.9 -m venv .venv && source .venv/bin/activate
   pip install -e .
   pip install fastapi uvicorn markdown
   ```

2. Pull Qwen3 8B in Ollama, then re-create it with a larger context window
   (the pipeline prompts exceed the default 8k context):

   ```bash
   ollama pull qwen3:8b
   ollama create qwen3:8b -f - <<'EOF'
   FROM qwen3:8b
   PARAMETER temperature 0.7
   PARAMETER num_ctx 32768
   EOF
   ```

   Thinking mode is disabled per-request by the `OllamaChatOpenAI` client
   (model-level `/no_think` directive), so no other flags are needed.

## Usage

### CLI run

```bash
cd examples/eastmoney_demo
../../.venv/bin/python run_demo.py
```

Edit `TICKER` / `DATE` in `run_demo.py` to analyze another stock (A-share
codes use `.SS` / `.SZ` suffixes, e.g. `000001.SZ`).

### Web console

```bash
cd examples/eastmoney_demo
../../.venv/bin/python web_serve.py
# open http://127.0.0.1:8790/
```

The console lets you pick ticker/date/debate rounds, shows the 12-agent
pipeline with live node states and a colored log stream, and links to the
report viewer (`/reports/`). Each run is archived under `reports/runs/`.

## Notes

- Data vendors: all categories route to the Eastmoney adapter (see
  `tradingagents/dataflows/eastmoney.py`); `prediction_markets` stays on
  Polymarket and degrades gracefully offline.
- Set `TRADINGAGENTS_HOME` to override the repo-root auto-detection if you
  run the scripts from a symlinked or relocated checkout.
- Runtime outputs (`results/`, `cache/`, `memory/`) are written under the
  repo root and are git-ignored.
