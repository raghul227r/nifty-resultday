"""
Stock Dashboard - local backend
- /api/search?q=...          -> symbol suggestions (NSE/BSE)
- /api/stock/<symbol>        -> screener-style details + intraday & 1Y chart data
- /api/documents/<symbol>    -> company filings (announcements, results, concalls, annual reports)
- /api/indices               -> list of benchmark indices available for the heatmap
- /api/heatmap?index=<key>   -> day-change heatmap for any benchmark index

Price data: Yahoo Finance via the unofficial `yfinance` library (can be delayed).
Index constituents: NSE's public index CSVs (fallback: built-in Nifty 50 list).
Documents: NSE corporate-filing endpoints (unofficial, may change or block).
For learning/personal use, not trading.
"""
import io
import math
import threading
import time
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ---- Benchmark indices: key -> (label, NSE constituents CSV, Yahoo index symbol or None)
INDICES = {
    "nifty50":          ("NIFTY 50",            "ind_nifty50list.csv",           "^NSEI"),
    "niftybank":        ("NIFTY Bank",          "ind_niftybanklist.csv",         "^NSEBANK"),
    "niftynext50":      ("NIFTY Next 50",       "ind_niftynext50list.csv",       "^NSMIDCP"),
    "nifty100":         ("NIFTY 100",           "ind_nifty100list.csv",          "^CNX100"),
    "nifty200":         ("NIFTY 200",           "ind_nifty200list.csv",          None),
    "nifty500":         ("NIFTY 500",           "ind_nifty500list.csv",          "^CRSLDX"),
    "niftymidcap100":   ("NIFTY Midcap 100",    "ind_niftymidcap100list.csv",    "^CNXMIDCAP"),
    "niftymidcap150":   ("NIFTY Midcap 150",    "ind_niftymidcap150list.csv",    None),
    "niftysmallcap100": ("NIFTY Smallcap 100",  "ind_niftysmallcap100list.csv",  "^CNXSC"),
    "niftysmallcap250": ("NIFTY Smallcap 250",  "ind_niftysmallcap250list.csv",  None),
    "niftymidsmall400": ("NIFTY MidSmallcap 400", "ind_niftymidsmallcap400list.csv", None),
    "niftyfin":         ("NIFTY Financial Services", "ind_niftyfinancelist.csv", "^CNXFIN"),
    "niftyit":          ("NIFTY IT",            "ind_niftyitlist.csv",           "^CNXIT"),
    "niftypharma":      ("NIFTY Pharma",        "ind_niftypharmalist.csv",       "^CNXPHARMA"),
    "niftyauto":        ("NIFTY Auto",          "ind_niftyautolist.csv",         "^CNXAUTO"),
    "niftyfmcg":        ("NIFTY FMCG",          "ind_niftyfmcglist.csv",         "^CNXFMCG"),
    "niftymetal":       ("NIFTY Metal",         "ind_niftymetallist.csv",        "^CNXMETAL"),
    "niftyenergy":      ("NIFTY Energy",        "ind_niftyenergylist.csv",       "^CNXENERGY"),
    "niftyrealty":      ("NIFTY Realty",        "ind_niftyrealtylist.csv",       "^CNXREALTY"),
}
ARCHIVE = "https://nsearchives.nseindia.com/content/indices/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Fallback if the NSE CSV can't be downloaded (edit when constituents change)
NIFTY50 = {
    "JSWSTEEL.NS": "JSW Steel", "ONGC.NS": "ONGC", "SBIN.NS": "SBI",
    "EICHERMOT.NS": "Eicher Motors", "LT.NS": "Larsen", "ADANIPORTS.NS": "Adani Ports",
    "DRREDDY.NS": "Dr Reddys Labs", "CIPLA.NS": "Cipla", "INDIGO.NS": "Interglobe Avi",
    "TCS.NS": "TCS", "MARUTI.NS": "Maruti Suzuki", "POWERGRID.NS": "Power Grid Corp",
    "BEL.NS": "Bharat Elec", "ASIANPAINT.NS": "Asian Paints", "SBILIFE.NS": "SBI Life Insura",
    "BAJAJ-AUTO.NS": "Bajaj Auto", "HINDALCO.NS": "Hindalco", "BSE.NS": "BSE Limited",
    "RELIANCE.NS": "Reliance", "HCLTECH.NS": "HCL Tech", "ICICIBANK.NS": "ICICI Bank",
    "M&M.NS": "M&M", "TATASTEEL.NS": "Tata Steel", "TITAN.NS": "Titan Company",
    "ITC.NS": "ITC", "JIOFIN.NS": "Jio Financial", "COALINDIA.NS": "Coal India",
    "KOTAKBANK.NS": "Kotak Mahindra", "SUNPHARMA.NS": "Sun Pharma", "TECHM.NS": "Tech Mahindra",
    "ETERNAL.NS": "Eternal", "HDFCLIFE.NS": "HDFC Life", "TATACONSUM.NS": "TATA Cons. Prod",
    "SHRIRAMFIN.NS": "Shriram Finance", "ADANIENT.NS": "Adani Enterpris", "GRASIM.NS": "Grasim",
    "BAJAJFINSV.NS": "Bajaj Finserv", "BHARTIARTL.NS": "Bharti Airtel", "MAXHEALTH.NS": "Max Healthcare",
    "NESTLEIND.NS": "Nestle", "HINDUNILVR.NS": "HUL", "NTPC.NS": "NTPC",
    "APOLLOHOSP.NS": "Apollo Hospital", "HDFCBANK.NS": "HDFC Bank", "BAJFINANCE.NS": "Bajaj Finance",
    "TRENT.NS": "Trent", "TMPV.NS": "TMPV", "ULTRACEMCO.NS": "UltraTechCement",
    "INFY.NS": "Infosys", "AXISBANK.NS": "Axis Bank",
}

# ---- tiny TTL cache so we don't hammer Yahoo / NSE --------------------------
_cache = {}


def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def clean(obj):
    """Make anything JSON-safe (NaN/inf -> None, numpy -> python)."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if hasattr(obj, "item"):
        try:
            obj = obj.item()
        except Exception:
            pass
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


# ---- routes: pages & search ---------------------------------------------------
@app.route("/")
def home():
    return send_from_directory("static", "index.html")


@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    out = []
    try:
        for r in yf.Search(q, max_results=12).quotes:
            sym = r.get("symbol", "")
            if sym.endswith((".NS", ".BO")):
                out.append({
                    "symbol": sym,
                    "name": r.get("shortname") or r.get("longname") or sym,
                    "exchange": "NSE" if sym.endswith(".NS") else "BSE",
                })
    except Exception:
        pass
    if not out:  # fallback: match against the local Nifty 50 list
        ql = q.lower()
        out = [{"symbol": s, "name": n, "exchange": "NSE"}
               for s, n in NIFTY50.items() if ql in n.lower() or ql in s.lower()]
    return jsonify(out)


def resolve(symbol):
    """'reliance' -> 'RELIANCE.NS'. Keeps explicit suffixes / index symbols."""
    s = symbol.strip().upper()
    if "." in s or s.startswith("^"):
        return s
    return s + ".NS"


# ---- routes: stock details ------------------------------------------------------
def intraday(t):
    df = t.history(period="5d", interval="5m")
    if df.empty:
        return []
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("Asia/Kolkata")
    last_day = df.index[-1].date()
    df = df[df.index.date == last_day]
    return [{"t": i.strftime("%H:%M"), "c": round(float(c), 2)}
            for i, c in zip(df.index, df["Close"])]


def daily(t):
    df = t.history(period="1y", interval="1d")
    return [{"t": i.strftime("%Y-%m-%d"), "c": round(float(c), 2)}
            for i, c in zip(df.index, df["Close"])]


def quarterly(t):
    try:
        q = t.quarterly_income_stmt
        rows = {}
        for label in ["Total Revenue", "Operating Income", "Net Income"]:
            if label in q.index:
                rows[label] = [
                    {"period": c.strftime("%b %Y"), "value": v}
                    for c, v in zip(q.columns, q.loc[label]) if pd.notna(v)
                ]
        return rows
    except Exception:
        return {}


@app.route("/api/stock/<path:symbol>")
def stock(symbol):
    sym = resolve(symbol)

    def build():
        t = yf.Ticker(sym)
        info = t.info or {}
        if not info or not (info.get("currentPrice") or info.get("regularMarketPrice")
                            or info.get("longName") or info.get("shortName")):
            return None
        keys = [
            "longName", "shortName", "sector", "industry", "website", "longBusinessSummary",
            "currentPrice", "regularMarketPrice", "previousClose", "open", "dayHigh", "dayLow",
            "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "volume", "averageVolume", "marketCap",
            "trailingPE", "forwardPE", "priceToBook", "trailingEps", "forwardEps", "bookValue",
            "dividendYield", "dividendRate", "payoutRatio", "beta",
            "returnOnEquity", "returnOnAssets", "profitMargins", "operatingMargins", "grossMargins",
            "revenueGrowth", "earningsGrowth", "debtToEquity", "currentRatio", "quickRatio",
            "totalRevenue", "ebitda", "totalCash", "totalDebt", "freeCashflow",
            "heldPercentInsiders", "heldPercentInstitutions",
            "recommendationKey", "targetMeanPrice", "numberOfAnalystOpinions", "currency",
        ]
        return {
            "symbol": sym,
            "info": {k: info.get(k) for k in keys},
            "intraday": intraday(t),
            "daily": daily(t),
            "quarterly": quarterly(t),
        }

    try:
        data = cached(("stock", sym), 60, build)
    except Exception as e:
        return jsonify({"error": f"Could not fetch {sym}: {e}"}), 502
    if data is None:
        return jsonify({"error": f"No data found for {sym}"}), 404
    return jsonify(clean(data))


# ---- routes: documents (company filings) --------------------------------------------
def nse_get(path, params):
    """NSE needs a browser-like session (cookies from the home page first)."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    s.get("https://www.nseindia.com", timeout=15)
    r = s.get("https://www.nseindia.com" + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def categorize(desc, text):
    d = f"{desc} {text}".lower()
    if "financial result" in d or "integrated filing" in d:
        return "Results"
    if any(k in d for k in ("investor meet", "con. call", "concall", "transcript",
                            "investor presentation", "earnings call", "audio recording")):
        return "Concalls & Presentations"
    return "Other"


@app.route("/api/documents/<path:symbol>")
def documents(symbol):
    base = resolve(symbol).split(".")[0]

    def build():
        errors, announcements, reports = [], [], []
        try:
            data = nse_get("/api/corporate-announcements",
                           {"index": "equities", "symbol": base})
            for a in (data if isinstance(data, list) else data.get("data", []))[:80]:
                desc, text = a.get("desc") or "", a.get("attchmntText") or ""
                announcements.append({
                    "date": a.get("an_dt") or a.get("sort_date") or "",
                    "title": desc, "text": text, "url": a.get("attchmntFile") or "",
                    "category": categorize(desc, text),
                })
        except Exception as e:
            errors.append(f"Announcements: {e}")
        try:
            data = nse_get("/api/annual-reports", {"index": "equities", "symbol": base})
            for a in (data if isinstance(data, list) else data.get("data", [])):
                reports.append({
                    "date": f"FY {a.get('fromYr', '')}-{a.get('toYr', '')}",
                    "title": "Annual Report", "text": a.get("companyName") or "",
                    "url": a.get("fileName") or "", "category": "Annual Reports",
                })
        except Exception as e:
            errors.append(f"Annual reports: {e}")
        return {
            "symbol": base,
            "documents": reports + announcements,
            "errors": errors,
            "links": {
                "Screener.in": f"https://www.screener.in/company/{base}/",
                "NSE filings": f"https://www.nseindia.com/get-quotes/equity?symbol={base}",
                "BSE filings": "https://www.bseindia.com/corporates/ann.html",
            },
        }

    result = cached(("docs", base), 900, build)
    if result["errors"] and not result["documents"]:
        _cache.pop(("docs", base), None)  # don't cache a total failure
    return jsonify(clean(result))


# ---- routes: heatmap for any benchmark index -----------------------------------------
_lock = threading.Lock()
_quotes = {}  # symbol -> (timestamp, {"price", "change", "pct"})


def _closes(data, sym):
    try:
        df = data[sym]
    except KeyError:  # single-ticker download comes back flat
        df = data
    return df["Close"].dropna()


def get_quotes(symbols, ttl=90):
    """Latest price and % change vs previous close, fetched in chunks and cached."""
    with _lock:
        now = time.time()
        need = [s for s in symbols if s not in _quotes or now - _quotes[s][0] > ttl]
        for i in range(0, len(need), 100):
            chunk = need[i:i + 100]
            try:
                data = yf.download(chunk, period="5d", interval="1d", group_by="ticker",
                                   progress=False, threads=True, auto_adjust=False)
            except Exception:
                continue
            for sym in chunk:
                try:
                    c = _closes(data, sym)
                    if len(c) < 2:
                        continue
                    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
                    _quotes[sym] = (time.time(), {
                        "price": round(last, 2), "change": round(last - prev, 2),
                        "pct": round((last / prev - 1) * 100, 2)})
                except Exception:
                    continue
        return {s: _quotes[s][1] for s in symbols if s in _quotes}


def short_name(n):
    for suffix in (" Limited", " Ltd.", " Ltd"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n.strip()


def constituents(key):
    label, csv_name, _ = INDICES[key]

    def build():
        r = requests.get(ARCHIVE + csv_name, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        return [(f"{row['Symbol']}.NS", short_name(str(row["Company Name"])),
                 str(row.get("Industry", "")))
                for _, row in df.iterrows()]

    try:
        return cached(("cons", key), 86400, build)
    except Exception:
        if key == "nifty50":
            return [(s, n, "") for s, n in NIFTY50.items()]
        raise


@app.route("/api/indices")
def indices():
    return jsonify([{"key": k, "label": v[0]} for k, v in INDICES.items()])


@app.route("/api/heatmap")
def heatmap():
    key = request.args.get("index", "nifty50")
    if key not in INDICES:
        return jsonify({"error": "Unknown index"}), 404
    label, _, yahoo_index = INDICES[key]
    try:
        cons = constituents(key)
    except Exception as e:
        return jsonify({"error": f"Could not load {label} constituents from NSE: {e}"}), 502
    try:
        syms = [c[0] for c in cons] + ([yahoo_index] if yahoo_index else [])
        q = get_quotes(syms)
        rows = [{"symbol": s, "name": n, "industry": ind, **q[s]}
                for s, n, ind in cons if s in q]
        index = {"symbol": yahoo_index, **q[yahoo_index]} if yahoo_index in q else None
        return jsonify(clean({
            "key": key, "label": label,
            "asOf": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            "index": index, "total": len(cons), "loaded": len(rows),
            "advances": sum(1 for r in rows if r["pct"] > 0),
            "declines": sum(1 for r in rows if r["pct"] < 0),
            "stocks": rows,
        }))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
