"""Local demo run of TradingAgents with Ollama (qwen3:8b) + Eastmoney A-share data.

Target: Kweichow Moutai (600519.SS) analyzed on 2026-08-21.
Data vendors: all categories route to the Eastmoney adapter (Yahoo is
geo-blocked from this network); macro_data uses Eastmoney CPI/PMI.
"""
import os
import sys

# Repository root (two levels up from examples/eastmoney_demo).
BASE = os.environ.get("TRADINGAGENTS_HOME") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, BASE)
os.environ["TRADINGAGENTS_RESULTS_DIR"] = os.path.join(BASE, "results")
os.environ["TRADINGAGENTS_CACHE_DIR"] = os.path.join(BASE, "cache")
os.environ["TRADINGAGENTS_MEMORY_LOG_PATH"] = os.path.join(BASE, "memory", "trading_memory.md")

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

TICKER = "600519.SS"
DATE = "2026-08-21"

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "ollama"
config["deep_think_llm"] = "qwen3:8b"
config["quick_think_llm"] = "qwen3:8b"
config["max_debate_rounds"] = 1
config["max_risk_discuss_rounds"] = 1
config["news_article_limit"] = 5
config["global_news_article_limit"] = 5
config["output_language"] = "Chinese"
# A-share routing: Eastmoney for every data category.
config["data_vendors"] = {
    "core_stock_apis": "eastmoney",
    "technical_indicators": "eastmoney",
    "fundamental_data": "eastmoney",
    "news_data": "eastmoney",
    "macro_data": "eastmoney",
    "prediction_markets": "polymarket",
}

ta = TradingAgentsGraph(debug=True, config=config)
final_state, decision = ta.propagate(TICKER, DATE)
print("\n=== FINAL DECISION ===")
print(decision)
ta.save_reports(final_state, TICKER, save_path=os.path.join(BASE, "results", "demo_reports"))
print("\n=== REPORTS SAVED ===")
