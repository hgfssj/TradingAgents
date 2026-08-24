"""Generate a self-contained report viewer page from TradingAgents markdown reports.

Builds task/tradingagents-demo/reports/index.html: a sidebar file tree + content
pane. No external CDN dependencies (works fully offline).
"""
import os
import re
import sys
from datetime import datetime

import markdown

BASE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(BASE, "reports")
OUT = os.path.join(REPORTS, "index.html")

# Pipeline stage order for the sidebar.
STAGE_ORDER = [
    ("1_analysts", "1. 分析师团队"),
    ("2_research", "2. 牛熊辩论"),
    ("3_trading", "3. 交易员"),
    ("4_risk", "4. 风险辩论"),
    ("5_portfolio", "5. 组合经理"),
]
NAME_ZH = {
    "fundamentals": "基本面分析师",
    "market": "市场分析师",
    "news": "新闻分析师",
    "sentiment": "情绪分析师",
    "bull": "多方观点",
    "bear": "空方观点",
    "manager": "研究经理",
    "trader": "交易员",
    "aggressive": "激进风险方",
    "conservative": "保守风险方",
    "neutral": "中性风险方",
    "decision": "最终决策",
    "complete_report": "完整报告",
}


def build_sidebar() -> list[tuple[str, str, str, str]]:
    """Return [(display_label, rel_path, section_id, task_key), ...] grouped by task.

    Task grouping: top-level stage dirs belong to the demo run (task_key="");
    anything under runs/<task_id>/ is one history task.
    """
    files: dict[str, list[str]] = {}
    for dirpath, _dirnames, filenames in os.walk(REPORTS):
        for fn in sorted(filenames):
            if not fn.endswith(".md"):
                continue
            rel_dir = os.path.relpath(dirpath, REPORTS)
            files.setdefault(rel_dir, []).append(fn)
    # Group stage dirs by task key.
    groups: dict[str, dict[str, list[str]]] = {}
    for rel_dir, fns in files.items():
        parts = rel_dir.split(os.sep)
        if parts and parts[0] == "runs" and len(parts) >= 2:
            task_key = os.sep.join(parts[:2])  # runs/<task_id>
            stage_dir = parts[-1] if len(parts) > 2 else "."
        else:
            task_key = ""
            stage_dir = parts[-1] if parts else "."
        groups.setdefault(task_key, {}).setdefault(stage_dir, []).extend(fns)

    def task_label(key: str) -> str:
        if not key:
            return "当前演示"
        try:
            ts = int(key.split(os.sep)[-1]) / 1000
            return f"任务 {datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')}"
        except (ValueError, OSError):
            return key

    items: list[tuple[str, str, str, str]] = []
    # Demo run first, then history tasks newest-first.
    order = [""] + sorted((k for k in groups if k), reverse=True)
    for task_key in order:
        task_files = groups.get(task_key, {})
        prefix = task_key if task_key else "."
        for stage_dir, stage_label in STAGE_ORDER:
            for fn in task_files.get(stage_dir, []):
                stem = fn[:-3]
                sid = re.sub(r"[^A-Za-z0-9_-]", "-", f"{task_key}-{stage_dir}-{stem}")
                label = NAME_ZH.get(stem, stem)
                items.append(
                    (f"{stage_label} · {label}", os.path.join(prefix, stage_dir, fn),
                     sid, task_label(task_key))
                )
        for fn in sorted(task_files.get(".", [])):
            stem = fn[:-3]
            sid = re.sub(r"[^A-Za-z0-9_-]", "-", f"{task_key}-root-{stem}")
            items.append(
                (NAME_ZH.get(stem, stem), os.path.join(prefix, fn), sid,
                 task_label(task_key))
            )
    return items


def render_md(path: str) -> str:
    text = open(path, encoding="utf-8").read()
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    # Make table column headers numeric-aligned for readability.
    return html


def main() -> None:
    items = build_sidebar()
    nav_entries, sections = [], []
    last_group: str | None = None
    for label, rel, sid, group in items:
        if group != last_group:
            nav_entries.append(f'<div class="nav-group">{group}</div>')
            last_group = group
        nav_entries.append(f'<a href="#{sid}" class="nav-item">{label}</a>')
        body = render_md(os.path.join(REPORTS, rel))
        sections.append(
            f'<section class="doc" id="{sid}"><h2 class="doc-title">{label}</h2>'
            f'<div class="doc-body">{body}</div></section>'
        )
    page = PAGE_TEMPLATE.format(
        nav="\n".join(nav_entries),
        content="\n".join(sections),
        count=len(items),
        title="TradingAgents 多智能体分析报告",
    )
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"generated {OUT} ({len(items)} docs)")


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg: #0f1115; --panel: #171a21; --border: #262b36; --text: #d7dbe2;
  --muted: #8a93a3; --accent: #4f8cff; --accent-soft: #1d2a45;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
       font: 15px/1.7 -apple-system, "PingFang SC", "Noto Sans SC", sans-serif; }}
header {{ position: sticky; top: 0; z-index: 10; background: var(--panel);
         border-bottom: 1px solid var(--border); padding: 14px 24px; }}
header h1 {{ margin: 0; font-size: 17px; font-weight: 600; }}
header p {{ margin: 4px 0 0; color: var(--muted); font-size: 12.5px; }}
.layout {{ display: flex; min-height: calc(100vh - 62px); }}
nav {{ width: 250px; flex-shrink: 0; border-right: 1px solid var(--border);
       padding: 18px 12px; position: sticky; top: 62px; height: calc(100vh - 62px);
       overflow-y: auto; }}
.nav-item {{ display: block; padding: 7px 12px; color: var(--text); text-decoration: none;
             border-radius: 8px; font-size: 13.5px; }}
.nav-item:hover {{ background: var(--accent-soft); color: #fff; }}
.nav-group {{ margin: 14px 8px 6px; padding-bottom: 4px; font-size: 11.5px;
              color: var(--muted); border-bottom: 1px solid var(--border);
              text-transform: uppercase; letter-spacing: .4px; }}
main {{ flex: 1; padding: 28px 40px 80px; max-width: 980px; }}
.doc-title {{ font-size: 20px; margin: 0 0 18px; padding-bottom: 10px;
              border-bottom: 1px solid var(--border); }}
.doc {{ margin-bottom: 56px; }}
.doc-body h1, .doc-body h2 {{ font-size: 18px; margin: 26px 0 10px; color: #fff; }}
.doc-body h3 {{ font-size: 16px; margin: 20px 0 8px; color: #e6e9ef; }}
.doc-body h4 {{ font-size: 14.5px; margin: 16px 0 6px; color: var(--text); }}
.doc-body p {{ margin: 10px 0; }}
.doc-body table {{ border-collapse: collapse; margin: 14px 0; width: 100%;
                   font-size: 13.5px; }}
.doc-body th, .doc-body td {{ border: 1px solid var(--border); padding: 7px 10px;
                              text-align: left; }}
.doc-body th {{ background: var(--accent-soft); color: #fff; font-weight: 600; }}
.doc-body tr:nth-child(even) {{ background: #14171d; }}
.doc-body code {{ background: #20242c; padding: 2px 6px; border-radius: 5px;
                  font-size: 13px; }}
.doc-body pre {{ background: #13161b; border: 1px solid var(--border);
                 padding: 14px; border-radius: 10px; overflow-x: auto; }}
.doc-body pre code {{ background: none; padding: 0; }}
.doc-body ul, .doc-body ol {{ padding-left: 24px; }}
.doc-body li {{ margin: 5px 0; }}
.doc-body blockquote {{ border-left: 3px solid var(--accent); margin: 14px 0;
                        padding: 8px 16px; background: #14171d; border-radius: 0 8px 8px 0; }}
.doc-body a {{ color: var(--accent); }}
.doc-body hr {{ border: none; border-top: 1px solid var(--border); margin: 22px 0; }}
</style>
</head>
<body>
<header>
  <h1>TradingAgents 多智能体分析报告</h1>
  <p>本机 Ollama qwen3:8b · 东方财富 A 股数据 · 共 {count} 份文档</p>
</header>
<div class="layout">
  <nav>{nav}</nav>
  <main>{content}</main>
</div>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
