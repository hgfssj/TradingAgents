"""A-share data vendor backed by Eastmoney public APIs (CN markets).

Implements the same vendor interface yfinance does so it can be registered
in ``interface.VENDOR_METHODS`` under the ``eastmoney`` key and selected via
``config["data_vendors"]``. Data paths:

- OHLCV:      push2his.eastmoney.com kline API (daily, qfq-adjusted)
- Realtime:   push2.eastmoney.com stock/get (price, name, PE)
- Financials: datacenter-web.eastmoney.com F10 statements (CN GAAP, CNY)
- News:       search-api-web.eastmoney.com article search
- Identity:   realtime quote f58 name (deterministic company-name anchor)

All functions return the same text shapes as the yfinance vendor so agents
and the report pipeline stay unchanged.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

from .errors import NoMarketDataError

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

_CN_SUFFIX_RE = re.compile(r"^(?P<code>\d{6})\.(?P<ex>SS|SZ)$", re.IGNORECASE)


def _to_secid(symbol: str) -> tuple[str, str]:
    """Map a Yahoo-style CN ticker to Eastmoney secid and bare code.

    600519.SS -> ('1.600519', '600519'); 000001.SZ -> ('0.000001', '000001')
    """
    m = _CN_SUFFIX_RE.match(str(symbol).strip())
    if not m:
        raise NoMarketDataError(symbol, symbol, "not a CN A-share ticker (use CODE.SS / CODE.SZ)")
    code = m.group("code")
    market = "1" if m.group("ex").upper() == "SS" else "0"
    return f"{market}.{code}", code


def _em_get(url: str, params: dict, timeout: int = 20) -> dict:
    """GET with retry; raises NoMarketDataError with a readable detail on failure.

    push2his occasionally drops connections (RemoteDisconnected); the numeric
    mirror hosts (e.g. 23.push2his.eastmoney.com) serve the same API, so they
    are rotated into the retry loop.
    """
    last_err: Exception | None = None
    urls = [url]
    if "push2his.eastmoney.com" in url:
        urls += [url.replace("push2his", f"{n}.push2his") for n in (23, 48, 90)]
    for attempt in range(8):
        try_url = urls[attempt % len(urls)]
        if attempt:
            time.sleep(1.2)  # the drop is intermittent; pace retries
        try:
            r = requests.get(try_url, params=params, headers=_HEADERS, timeout=timeout)
            if r.status_code == 200:
                txt = r.text.strip()
                if txt and txt[0] not in "[{":
                    # JSONP envelope like ``x({...});`` — strip the callback.
                    inner = txt[txt.find("(") + 1 : txt.rfind(")")]
                    return json.loads(inner)
                return r.json()
            last_err = RuntimeError(f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise NoMarketDataError(str(params), detail=f"Eastmoney request failed: {last_err}")


# --- OHLCV -----------------------------------------------------------------


def download_ohlcv_em(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Daily adjusted OHLCV as a DataFrame with Date/Open/High/Low/Close/Volume."""
    secid, code = _to_secid(symbol)
    # push2his kills connections when ``end`` exceeds the latest trading day
    # (yfinance tolerates exclusive future ends; this API does not), so clamp.
    today = datetime.now().strftime("%Y-%m-%d")
    if end > today:
        end = today
    beg = start.replace("-", "")
    endd = end.replace("-", "")

    def _kline_window(b: str, e: str) -> list[str]:
        data = _em_get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "klt": "101",
                "fqt": "1",
                "beg": b,
                "end": e,
            },
        )
        return (data.get("data") or {}).get("klines") or []

    # Wide windows (~>1y) make push2his drop the connection reliably, while
    # ~6-month windows succeed; chunk the request and merge.
    s_dt, e_dt = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    if (e_dt - s_dt).days <= 370:
        klines = _kline_window(beg, endd)
    else:
        klines, seen = [], set()
        cur = s_dt
        while cur <= e_dt:
            seg_end = min(cur + timedelta(days=180), e_dt)
            for row in _kline_window(cur.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d")):
                day = row.split(",", 1)[0]
                if day not in seen:
                    seen.add(day)
                    klines.append(row)
            cur = seg_end + timedelta(days=1)
    if not klines:
        raise NoMarketDataError(symbol, symbol, "no kline rows returned")
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 6:
            continue
        rows.append({
            "Date": p[0],
            "Open": float(p[1]),
            "Close": float(p[2]),
            "High": float(p[3]),
            "Low": float(p[4]),
            "Volume": float(p[5]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise NoMarketDataError(symbol, symbol, "empty kline frame")
    return df


def get_stock_data(symbol, start_date, end_date):
    """OHLCV as a CSV string (same shape as the yfinance vendor)."""
    df = download_ohlcv_em(symbol, start_date, end_date)
    header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + df.to_csv(index=False)


# --- Realtime / identity ---------------------------------------------------


def _get_realtime(symbol: str) -> dict:
    """Latest quote row via the ulist batch API (stock/get is frequently
    connection-dropped; ulist.np/get is the stable public quote endpoint).

    Field map: f2 price, f14 name, f9 dynamic PE, f115 TTM PE, f23 PB,
    f20 total market cap, f3 pct change.
    """
    secid, _ = _to_secid(symbol)
    data = _em_get(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        {"fltt": "2", "secids": secid, "fields": "f2,f3,f9,f14,f20,f23,f115"},
    )
    diff = ((data.get("data") or {}).get("diff")) or []
    return diff[0] if diff else {}


def resolve_identity_em(ticker: str) -> dict:
    """Deterministic identity for CN tickers: name + exchange + basic ratios."""
    try:
        rt = _get_realtime(ticker)
    except NoMarketDataError:
        return {}
    identity = {"exchange": "Shanghai" if ticker.upper().endswith(".SS") else "Shenzhen"}
    name = rt.get("f14")
    if name:
        identity["company_name"] = str(name)
    price = rt.get("f2")
    if isinstance(price, (int, float)) and price > 0:
        identity["current_price"] = f"{price:.2f}"
    pe = rt.get("f115") or rt.get("f9")
    if isinstance(pe, (int, float)) and pe > 0:
        identity["pe_ratio"] = f"{pe:.2f}"
    pb = rt.get("f23")
    if isinstance(pb, (int, float)) and pb > 0:
        identity["pb_ratio"] = f"{pb:.2f}"
    return identity


# --- Financial statements (CN GAAP) ----------------------------------------


_F10_REPORTS = {
    "main": "RPT_F10_FINANCE_MAINFINADATA",
    "balance": "RPT_F10_FINANCE_GBALANCE",
    "income": "RPT_F10_FINANCE_GINCOME",
    "cashflow": "RPT_F10_FINANCE_GCASHFLOW",
}


def _f10_rows(symbol: str, report: str, page_size: int = 6) -> list[dict]:
    secid, code = _to_secid(symbol)
    secucode = f"{code}.{'SH' if secid.startswith('1.') else 'SZ'}"
    data = _em_get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        {
            "reportName": _F10_REPORTS[report],
            "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": 1,
            "pageSize": page_size,
            "sortTypes": -1,
            "sortColumns": "REPORT_DATE",
            "source": "WEB",
            "client": "WEB",
        },
    )
    return (data.get("result") or {}).get("data") or []


# English key -> Chinese label for the most important CN-statement fields.
_FIELD_LABELS: dict[str, str] = {
    # main indicators
    "EPSJB": "基本每股收益(元)", "EPSXS": "稀释每股收益(元)", "BPS": "每股净资产(元)",
    "ROEJQ": "净资产收益率(加权%)", "XSMLL": "销售毛利率(%)", "XSJLL": "销售净利率(%)",
    "TOTALOPERATEREVE": "营业总收入(元)", "YSTZ": "营收同比(%)",
    "PARENTNETPROFIT": "归母净利润(元)", "SJLTZ": "净利润同比(%)",
    "KCFJCXSYJLR": "扣非净利润(元)", "ASSIGNDSCRPT": "每股经营现金流(元)",
    "TOTALEQUITY": "股东权益(元)", "TOTALASSETS": "资产总计(元)",
    "MGZBGJ": "每股资本公积(元)", "MGWFPLR": "每股未分配利润(元)",
    # balance
    "MONETARYFUNDS": "货币资金(元)", "TRADEACCOUNTSRECEIVABLE": "应收账款(元)",
    "INVENTORY": "存货(元)", "TOTALCURRENTASSETS": "流动资产合计(元)",
    "TOTALCURRENTLIAB": "流动负债合计(元)", "TOTALLIABILITIES": "负债合计(元)",
    "TOTALPARENTEQUITY": "归母所有者权益(元)",
    # income
    "OPERATEREVE": "营业收入(元)", "TOTALOPERATEEXP": "营业总成本(元)",
    "OPERATEPROFIT": "营业利润(元)", "TOTALPROFIT": "利润总额(元)",
    "NETPROFIT": "净利润(元)", "RESEARCHANDDEVELOPEEXP": "研发费用(元)",
    # cashflow
    "NETCASHOPERATE": "经营活动现金流量净额(元)",
    "NETCASHINVEST": "投资活动现金流量净额(元)",
    "NETCASHFINANCE": "筹资活动现金流量净额(元)",
    "CCEADD": "现金净增加额(元)", "CCEE": "期末现金及现金等价物(元)",
    "SALESERVICERENDER": "销售商品提供劳务收到的现金(元)",
}


def _render_f10(symbol: str, report: str, title: str, freq: str, max_rows: int = 4) -> str:
    rows = _f10_rows(symbol, report)
    if not rows:
        raise NoMarketDataError(symbol, symbol, f"no {report} rows from Eastmoney")
    lines = [f"# {title} for {symbol.upper()} (CN GAAP, CNY)", ""]
    for row in rows[:max_rows]:
        period = str(row.get("REPORT_DATE", ""))[:10]
        rtype = row.get("REPORT_DATE_NAME") or row.get("REPORT_TYPE") or ""
        lines.append(f"## {period} ({rtype})")
        for key, label in _FIELD_LABELS.items():
            val = row.get(key)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                continue
            lines.append(f"- {label}: {val}")
        lines.append("")
    if not any(l.startswith("- ") for l in lines):
        raise NoMarketDataError(symbol, symbol, f"no mapped fields in {report}")
    return "\n".join(lines)


def get_fundamentals(ticker, curr_date=None):
    return _render_f10(ticker, "main", "Company Fundamentals", "quarterly")


def get_balance_sheet(ticker, freq="quarterly", curr_date=None):
    return _render_f10(ticker, "balance", "Balance Sheet", freq)


def get_cashflow(ticker, freq="quarterly", curr_date=None):
    return _render_f10(ticker, "cashflow", "Cash Flow", freq)


def get_income_statement(ticker, freq="quarterly", curr_date=None):
    return _render_f10(ticker, "income", "Income Statement", freq)


# --- News ------------------------------------------------------------------


def _search_news(keyword: str, page_size: int = 10, page_index: int = 1, sort: str = "time") -> list[dict]:
    param = json.dumps({
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": sort,
            "pageIndex": page_index, "pageSize": page_size,
            "preTag": "<em>", "postTag": "</em>"}},
    })
    data = _em_get("https://search-api-web.eastmoney.com/search/jsonp", {"cb": "x", "param": param})
    return (data.get("result") or {}).get("cmsArticleWebOld") or []


def _search_news_window(keyword: str, max_pages: int = 3, page_size: int = 20, sort: str = "time") -> list[dict]:
    """Collect articles across pages (search returns newest first; a historical
    analysis window needs earlier pages)."""
    merged: list[dict] = []
    for idx in range(1, max_pages + 1):
        try:
            page = _search_news(keyword, page_size=page_size, page_index=idx, sort=sort)
        except NoMarketDataError:
            break
        if not page:
            break
        merged.extend(page)
    return merged


def _render_news(articles: list[dict], start_date: str, end_date: str, limit: int) -> str:
    lines, kept = [], 0
    for art in articles:
        date = (art.get("date") or "")[:10]
        if start_date and date and date < start_date:
            continue
        if end_date and date and date > end_date:
            continue
        if kept >= limit:
            break
        title = re.sub(r"</?em>", "", art.get("title") or "")
        url = art.get("url") or ""
        lines.append(f"- [{date}] {title} ({url})")
        kept += 1
    if not lines:
        return "No news articles found for the requested period."
    return "\n".join(lines)


def get_news(ticker, start_date, end_date):
    ident = resolve_identity_em(ticker)
    if "company_name" not in ident:
        ident = resolve_identity_em(ticker)  # one retry; ulist drops connections rarely
    name = ident.get("company_name") or _to_secid(ticker)[1]
    articles = _search_news_window(name)
    body = _render_news(articles, start_date, end_date, limit=10)
    return f"# News for {ticker.upper()} ({name}) from {start_date} to {end_date}\n\n{body}"


def get_global_news(curr_date, look_back_days=None, limit=None):
    look_back = look_back_days or 7
    limit = limit or 10
    start = (datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back)).strftime("%Y-%m-%d")
    # relevance-sorted search spans a multi-week window; the time-sorted feed
    # is dominated by intraday flash items and never reaches historical dates.
    articles = _search_news_window("宏观经济", max_pages=2, sort="default")
    body = _render_news(articles, start, curr_date, limit)
    return f"# Global/Macro news (CN) from {start} to {curr_date}\n\n{body}"


def get_insider_transactions(ticker):
    return (
        f"No insider transactions reported for symbol '{ticker}' "
        "(CN A-share insider-holding disclosures are not covered by this vendor; "
        "do not fabricate any insider figures)."
    )

# --- Macro indicators (CN) -------------------------------------------------


def get_macro_indicators(indicator, curr_date=None, look_back_days=None):
    """CN macro series from the Eastmoney datacenter (CPI / PMI).

    Accepts friendly aliases ("cpi", "inflation", "pmi"); anything else gets a
    short coverage note instead of a fabricated series. Returns the most
    recent monthly observations as markdown.
    """
    key = str(indicator or "").strip().lower()
    if key in ("cpi", "inflation", "cpi_yoy", "consumer prices"):
        report, title = "RPT_ECONOMY_CPI", "China CPI (national)"
        fields = [("NATIONAL_SAME", "同比(%)"), ("NATIONAL_SEQUENTIAL", "环比(%)")]
    elif key in ("pmi", "manufacturing_pmi"):
        report, title = "RPT_ECONOMY_PMI", "China Manufacturing PMI"
        fields = [("MAKE_INDEX", "制造业PMI")]
    else:
        return (
            f"Eastmoney macro vendor covers CPI and PMI only (requested: '{indicator}'). "
            "Ask for 'cpi' or 'pmi'; do not invent other macro series."
        )

    data = _em_get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        {
            "reportName": report,
            "columns": "ALL",
            "pageNumber": 1,
            "pageSize": 12,
            "sortColumns": "REPORT_DATE",
            "sortTypes": -1,
            "source": "WEB",
            "client": "WEB",
        },
    )
    rows = (data.get("result") or {}).get("data") or []
    if not rows:
        raise NoMarketDataError(indicator, report, "no macro rows from Eastmoney")
    lines = [f"# {title}", "", "| 月份 | " + " | ".join(lbl for _, lbl in fields) + " |",
             "|---|" + "---|" * len(fields)]
    for row in rows:
        period = (row.get("TIME") or str(row.get("REPORT_DATE", ""))[:10])[:10]
        vals = []
        for key, _ in fields:
            v = row.get(key)
            vals.append("N/A" if v is None else f"{v}")
        lines.append(f"| {period} | " + " | ".join(vals) + " |")
    return "\n".join(lines)

