# Stock Dashboard (local)

Search any NSE/BSE stock for screener-style details, today's chart and company documents (announcements, results, concalls, annual reports), plus a day heatmap for 19 benchmark indices (Nifty 50, Bank, Next 50, 100, 200, 500, Midcap, Smallcap, sector indices).

## Run it

1. Install Python 3.9+ (check: `python3 --version`)
2. In this folder:

   ```bash
   python3 -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```
3. Open http://127.0.0.1:5000

## Layout

```
stock-dashboard/
  app.py            Flask backend (search, stock details, heatmap APIs)
  requirements.txt
  static/index.html UI (search, screener, chart, heatmap)
```

## Notes

- Data comes from Yahoo Finance via `yfinance` (unofficial, can be delayed or rate-limited). Fine for learning, not for trading.
- Index constituents are downloaded from NSE's public CSVs and cached for a day; `NIFTY50` in `app.py` is only a fallback.
- NIFTY 500 loads ~500 tickers, so the first load can take up to a minute; later loads use a 90-second cache.
- Documents come from NSE's unofficial filing endpoints, which may block or change. The page also shows links to Screener.in, NSE and BSE as a fallback.
- Chart.js loads from a CDN, so you need internet access.
