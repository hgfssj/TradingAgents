"""Web control panel for TradingAgents (local Ollama + Eastmoney A-share data).

Runs the multi-agent pipeline in a background thread and streams node-level
progress to the browser via graph.stream(stream_mode="updates").

Endpoints:
  GET  /                  control-panel page (web/index.html)
  POST /api/run           start an analysis task
  GET  /api/tasks/{id}    poll task status (logs, node states, stats)
  GET  /api/reports       list generated report trees
  GET  /reports           regenerated static report viewer
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Repository root (two levels up from examples/eastmoney_demo).
BASE = os.environ.get("TRADINGAGENTS_HOME") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, BASE)
os.environ.setdefault("TRADINGAGENTS_RESULTS_DIR", os.path.join(BASE, "results"))
os.environ.setdefault("TRADINGAGENTS_CACHE_DIR", os.path.join(BASE, "cache"))
os.environ.setdefault(
    "TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(BASE, "memory", "trading_memory.md")
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
REPORTS_DIR = os.path.join(APP_DIR, "reports")
RUNS_DIR = os.path.join(REPORTS_DIR, "runs")
os.makedirs(RUNS_DIR, exist_ok=True)

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402

from tradingagents.graph.trading_graph import TradingAgentsGraph  # noqa: E402
from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402

# --- Node names (LangGraph) -> display labels -------------------------------

NODE_LABELS = {
    "Market Analyst": "市场分析师",
    "Sentiment Analyst": "情绪分析师",
    "News Analyst": "新闻分析师",
    "Fundamentals Analyst": "基本面分析师",
    "Bull Researcher": "多方研究员",
    "Bear Researcher": "空方研究员",
    "Research Manager": "研究经理",
    "Trader": "交易员",
    "Aggressive Analyst": "激进风险分析师",
    "Neutral Analyst": "中性风险分析师",
    "Conservative Analyst": "保守风险分析师",
    "Portfolio Manager": "组合经理",
}
NODE_ORDER = list(NODE_LABELS.keys())
TOOL_NODE_RE = re.compile(r"^tools_", re.IGNORECASE)

STAGE_CN = {
    "1_analysts": "1. 分析师团队",
    "2_research": "2. 牛熊辩论",
    "3_trading": "3. 交易员",
    "4_risk": "4. 风险辩论",
    "5_portfolio": "5. 组合经理",
}


class StatsHandler(BaseCallbackHandler):
    """Tracks LLM calls / tokens / tool calls from the LLM layer."""

    def __init__(self, task: "Task"):
        self.task = task
        self._lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, **kwargs):
        with self._lock:
            self.task.stats["llm_calls"] += 1

    def on_llm_end(self, response, **kwargs):
        try:
            msg = response.generations[0][0].message
            usage = getattr(msg, "usage_metadata", None) or {}
        except (IndexError, TypeError, AttributeError):
            return
        with self._lock:
            self.task.stats["tokens_in"] += usage.get("input_tokens", 0)
            self.task.stats["tokens_out"] += usage.get("output_tokens", 0)

    def on_tool_start(self, serialized, input_str, **kwargs):
        name = (serialized or {}).get("name", "tool")
        self.task.push_log("tool", f"工具调用: {name}")


class Task:
    def __init__(self, ticker: str, date: str, config: dict[str, Any]):
        self.id = f"{int(time.time() * 1000)}"
        self.ticker = ticker
        self.date = date
        self.config = config
        self.status = "queued"  # queued | running | done | error
        self.node_states: dict[str, str] = {n: "pending" for n in NODE_LABELS}
        self.logs: deque[dict] = deque(maxlen=600)
        self.log_seq = 0
        self.stats = {"llm_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0}
        self.decision: str | None = None
        self.report_dir: str | None = None
        self.error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._lock = threading.Lock()

    def push_log(self, level: str, text: str) -> None:
        with self._lock:
            self.log_seq += 1
            self.logs.append(
                {"seq": self.log_seq, "t": datetime.now().strftime("%H:%M:%S"),
                 "level": level, "text": text[:400]}
            )

    def set_node(self, node: str, state: str) -> None:
        if node in self.node_states:
            self.node_states[node] = state
            label = NODE_LABELS[node]
            self.push_log("node", f"{label} → {state}")
            if state == "completed":
                # Advance the "running" cursor to the next pending node so the
                # UI can show the active stage like the CLI spinner panel.
                for n in NODE_ORDER:
                    if self.node_states[n] == "pending":
                        self.node_states[n] = "running"
                        break

    def snapshot(self, since: int) -> dict[str, Any]:
        with self._lock:
            logs = [l for l in self.logs if l["seq"] > since]
            return {
                "id": self.id,
                "status": self.status,
                "ticker": self.ticker,
                "date": self.date,
                "node_states": dict(self.node_states),
                "logs": logs,
                "stats": dict(self.stats),
                "decision": self.decision,
                "report_dir": self.report_dir,
                "error": self.error,
                "elapsed": (
                    round((self.finished_at or time.time()) - self.started_at, 1)
                    if self.started_at else 0
                ),
            }


TASKS: dict[str, Task] = {}
TASKS_LOCK = threading.Lock()


def build_config(ticker: str, overrides: dict[str, Any]) -> dict[str, Any]:
    """DEFAULT_CONFIG + A-share/local defaults + user overrides."""
    config = DEFAULT_CONFIG.copy()
    config.update({
        "llm_provider": overrides.get("llm_provider", "ollama"),
        "deep_think_llm": overrides.get("deep_think_llm", "qwen3:8b"),
        "quick_think_llm": overrides.get("quick_think_llm", "qwen3:8b"),
        "max_debate_rounds": int(overrides.get("max_debate_rounds", 1)),
        "max_risk_discuss_rounds": int(overrides.get("max_risk_discuss_rounds", 1)),
        "output_language": overrides.get("output_language", "Chinese"),
        "data_vendors": {
            "core_stock_apis": "eastmoney",
            "technical_indicators": "eastmoney",
            "fundamental_data": "eastmoney",
            "news_data": "eastmoney",
            "macro_data": "eastmoney",
            "prediction_markets": "polymarket",
        },
    })
    # Extra passthrough: allow tuning news limits etc. from the UI.
    for key in ("news_article_limit", "global_news_article_limit", "selected_analysts"):
        if key in overrides and overrides[key] is not None:
            config[key] = overrides[key]
    return config


def run_task(task: Task) -> None:
    """Execute the pipeline in this (background) thread, streaming node events."""
    task.status = "running"
    task.started_at = time.time()
    try:
        config = build_config(task.ticker, task.config)
        handler = StatsHandler(task)
        ta = TradingAgentsGraph(debug=False, config=config, callbacks=[handler])

        # Same initialization as TradingAgentsGraph.propagate(), then stream with
        # per-node deltas so the UI gets node-level progress.
        ta.ticker = task.ticker
        ta._resolve_pending_entries(task.ticker)
        past_context = ta.memory_log.get_past_context(task.ticker)
        instrument_context = ta.resolve_instrument_context(task.ticker, "stock")
        init_state = ta.propagator.create_initial_state(
            task.ticker, task.date, "stock", past_context, instrument_context
        )
        args = {
            "stream_mode": "updates",
            "config": {"recursion_limit": ta.propagator.max_recur_limit},
        }

        task.push_log("sys", f"启动分析: {task.ticker} @ {task.date}")
        # AgentState extends MessagesState (messages is an add-reducer), so
        # stream(stream_mode="updates") chunks carry per-node deltas: append
        # messages and overwrite the rest, mirroring graph.invoke()'s result.
        final_state: dict[str, Any] = dict(init_state)
        for chunk in ta.graph.stream(init_state, **args):
            if not chunk:
                continue
            for node, delta in chunk.items():
                if not isinstance(delta, dict):
                    continue
                if TOOL_NODE_RE.match(node):
                    # Tool node output: count tool messages as tool calls.
                    msgs = delta.get("messages", [])
                    for m in msgs:
                        if isinstance(m, ToolMessage):
                            with handler._lock:
                                task.stats["tool_calls"] += 1
                    task.push_log("tool", f"工具节点完成: {node} ({len(msgs)} 条消息)")
                else:
                    task.set_node(node, "completed")
                msgs = delta.get("messages")
                if msgs:
                    final_state.setdefault("messages", []).extend(msgs)
                for k, v in delta.items():
                    if k != "messages":
                        final_state[k] = v

        if "final_trade_decision" not in final_state:
            raise RuntimeError("图未产出 final_trade_decision，运行不完整")

        decision = ta.process_signal(final_state["final_trade_decision"])
        task.decision = decision
        task.report_dir = os.path.join(RUNS_DIR, task.id)
        ta.save_reports(final_state, task.ticker, save_path=task.report_dir)
        # Keep the memory log consistent with a normal propagate() run.
        ta._log_state(task.date, final_state)
        ta.memory_log.store_decision(
            ticker=task.ticker, trade_date=task.date,
            final_trade_decision=final_state["final_trade_decision"],
        )
        task.push_log("sys", f"最终决策: {decision}")
        task.status = "done"
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        task.status = "error"
        task.error = f"{type(exc).__name__}: {exc}"
        task.push_log("err", task.error)
    finally:
        task.finished_at = time.time()
        regenerate_report_page()


def regenerate_report_page() -> None:
    """Rebuild reports/index.html so new runs appear in the viewer."""
    try:
        import subprocess
        subprocess.run(
            [sys.executable, os.path.join(APP_DIR, "make_report_page.py")],
            capture_output=True, timeout=120,
        )
    except Exception:  # noqa: BLE001 — viewer refresh is best-effort
        pass


# --- FastAPI app ------------------------------------------------------------

app = FastAPI(title="TradingAgents Web")


class RunRequest(BaseModel):
    ticker: str
    date: str
    overrides: dict[str, Any] = {}


@app.post("/api/run")
def api_run(req: RunRequest) -> JSONResponse:
    ticker = (req.ticker or "").strip().upper()
    date = (req.date or "").strip()
    if not re.match(r"^\d{6}\.(SS|SZ)$", ticker):
        raise HTTPException(400, "ticker 需为 A 股格式，如 600519.SS / 000001.SZ")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(400, "date 需为 YYYY-MM-DD")
    task = Task(ticker, date, req.overrides or {})
    with TASKS_LOCK:
        TASKS[task.id] = task
    task.push_log("sys", "任务已创建，等待启动")
    threading.Thread(target=run_task, args=(task,), daemon=True).start()
    return JSONResponse({"task_id": task.id})


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str, since: int = 0) -> JSONResponse:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    snap = task.snapshot(since)
    if task.status == "done" and snap["report_dir"]:
        snap["report_url"] = f"/reports/?task={task_id}"
    return JSONResponse(snap)


@app.get("/api/reports")
def api_reports() -> JSONResponse:
    runs = []
    for tid in sorted(os.listdir(RUNS_DIR), reverse=True):
        path = os.path.join(RUNS_DIR, tid)
        if not os.path.isdir(path):
            continue
        n = sum(1 for _, _, fs in os.walk(path) for f in fs if f.endswith(".md"))
        runs.append({"task_id": tid, "files": n})
    return JSONResponse({"runs": runs})


@app.get("/api/nodes")
def api_nodes() -> JSONResponse:
    return JSONResponse({"order": NODE_ORDER, "labels": NODE_LABELS})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


# Report viewer is regenerated as static files under reports/.
app.mount("/reports", StaticFiles(directory=REPORTS_DIR, html=True), name="reports")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8790, log_level="warning")
