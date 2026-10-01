"""Thin client for NSE's public JSON endpoints (www.nseindia.com/api/...).

NSE requires browser-like headers and the cookies it sets on the home page,
so the session is warmed up first and refreshed whenever NSE rejects a call.
"""
import time
import threading
from urllib.parse import quote

import requests

BASE_URL = "https://www.nseindia.com"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

SESSION_MAX_AGE = 240        # seconds before cookies are refreshed
MIN_GAP_BETWEEN_CALLS = 0.35  # be polite, NSE blocks aggressive clients


class NSEError(Exception):
    pass


class NSEClient:
    def __init__(self, timeout=15, retries=3):
        self.timeout = timeout
        self.retries = retries
        self._session = None
        self._session_born = 0
        self._last_call = 0
        self._lock = threading.Lock()

    # ---------- session ----------
    def _new_session(self):
        s = requests.Session()
        s.headers.update(HEADERS)
        # Home page + a market page set the cookies (nsit, nseappid, bm_sv ...)
        s.get(BASE_URL, timeout=self.timeout)
        s.get(f"{BASE_URL}/market-data/live-equity-market", timeout=self.timeout)
        self._session = s
        self._session_born = time.time()

    def _ensure_session(self, force=False):
        if force or self._session is None or time.time() - self._session_born > SESSION_MAX_AGE:
            self._new_session()

    def get(self, path, referer="/market-data/live-equity-market"):
        url = f"{BASE_URL}{path}"
        last_err = None
        with self._lock:
            for attempt in range(self.retries):
                try:
                    self._ensure_session(force=attempt > 0)
                    wait = MIN_GAP_BETWEEN_CALLS - (time.time() - self._last_call)
                    if wait > 0:
                        time.sleep(wait)
                    self._last_call = time.time()
                    r = self._session.get(url, headers={"Referer": BASE_URL + referer},
                                          timeout=self.timeout)
                    if r.status_code == 200 and r.text.strip():
                        return r.json()
                    last_err = NSEError(f"HTTP {r.status_code} for {path}")
                except (requests.RequestException, ValueError) as e:
                    last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise NSEError(f"NSE request failed: {path}: {last_err}")

    # ---------- endpoints ----------
    def market_status(self):
        return self.get("/api/marketStatus")

    def index_constituents(self, index_name):
        """Live quote for every stock in an index (one call for the whole universe)."""
        data = self.get(f"/api/equity-stockIndices?index={quote(index_name)}")
        rows = data.get("data", [])
        # The first row is the index itself
        return [r for r in rows if r.get("symbol") and r.get("symbol") != index_name
                and r.get("priority", 0) != 1]

    def preopen(self, key="FO"):
        data = self.get(f"/api/market-data-pre-open?key={key}", referer="/market-data/pre-open-market-cm-and-emerge-market")
        out = []
        for item in data.get("data", []):
            meta = item.get("metadata", {})
            if meta.get("symbol"):
                out.append(meta)
        return out

    def oi_spurts(self):
        """Combined futures + options OI change for every F&O underlying."""
        data = self.get("/api/live-analysis-oi-spurts-underlyings", referer="/market-data/oi-spurts")
        return data.get("data", [])

    def stock_futures(self):
        """Most active stock futures (LTP, OI, volume)."""
        data = self.get("/api/liveEquity-derivatives?index=stock_fut", referer="/market-data/equity-derivatives-watch")
        return data.get("data", [])

    def option_chain(self, symbol):
        """Nearest-expiry option chain for an equity. Tries the current (v3) API first,
        then the legacy one."""
        ref = "/option-chain"
        sym = quote(symbol)
        try:
            info = self.get(f"/api/option-chain-contract-info?symbol={sym}", referer=ref)
            expiries = info.get("expiryDates") or []
            if expiries:
                return self.get(f"/api/option-chain-v3?type=Equity&symbol={sym}&expiry={quote(expiries[0])}",
                                referer=ref)
        except NSEError:
            pass
        return self.get(f"/api/option-chain-equities?symbol={sym}", referer=ref)
