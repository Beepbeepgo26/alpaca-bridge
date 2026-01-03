import os
import threading
from typing import Dict, Any, Optional, List

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# Alpaca websocket (alpaca-py)
from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed


app = FastAPI(
    title="Alpaca Bridge API",
    description="Bridge between Alpaca market data and GPT Actions (REST bars + SIP websocket cache).",
    version="2.0.0",
)

# Allow GPT Actions to call this from anywhere
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- ENV HELPERS ----

def getenv_any(*names: str, default: Optional[str] = None) -> Optional[str]:
    """Try multiple env var names (helps when you accidentally used ALPACA_* vs APCA_*)."""
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v
    return default


APCA_KEY = getenv_any("APCA_API_KEY_ID", "ALPACA_API_KEY_ID")
APCA_SECRET = getenv_any("APCA_API_SECRET_KEY", "ALPACA_API_SECRET_KEY")

# IMPORTANT: this should be https://data.alpaca.markets (no /v2)
APCA_BASE_URL = getenv_any("APCA_BASE_URL", "ALPACA_BASE_URL", default="https://data.alpaca.markets").rstrip("/")

ALPACA_DATA_FEED = getenv_any("ALPACA_DATA_FEED", default="sip").lower()
SIP_SYMBOLS = getenv_any("SIP_SYMBOLS", default="SPY")

# ---- IN-MEMORY CACHE FROM WEBSOCKET ----
latest_trades: Dict[str, Dict[str, Any]] = {}
latest_trades_lock = threading.Lock()


def _alpaca_headers() -> Dict[str, str]:
    if not APCA_KEY or not APCA_SECRET:
        raise RuntimeError("Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY env vars.")
    return {
        "APCA-API-KEY-ID": APCA_KEY,
        "APCA-API-SECRET-KEY": APCA_SECRET,
    }


def _feed_enum(feed: str) -> DataFeed:
    # alpaca-py uses DataFeed enum
    if feed.lower() == "sip":
        return DataFeed.SIP
    return DataFeed.IEX


def start_sip_stream_thread() -> None:
    """Start Alpaca websocket stream in a background thread so FastAPI isn't blocked."""
    if not APCA_KEY or not APCA_SECRET:
        print("[stream] Keys not set; websocket stream will NOT start.")
        return

    symbols: List[str] = [s.strip().upper() for s in (SIP_SYMBOLS or "SPY").split(",") if s.strip()]
    feed = _feed_enum(ALPACA_DATA_FEED)

    def run_stream() -> None:
        try:
            stream = StockDataStream(APCA_KEY, APCA_SECRET, feed=feed)

            async def on_trade(trade) -> None:
                sym = str(trade.symbol).upper()
                payload = {
                    "symbol": sym,
                    "price": float(trade.price),
                    "size": int(getattr(trade, "size", 0) or 0),
                    "timestamp": str(trade.timestamp),
                    "feed": ALPACA_DATA_FEED,
                }
                with latest_trades_lock:
                    latest_trades[sym] = payload

            for sym in symbols:
                stream.subscribe_trades(on_trade, sym)

            print(f"[stream] Starting websocket stream. feed={ALPACA_DATA_FEED} symbols={symbols}")
            stream.run()
        except Exception as e:
            print(f"[stream] Websocket stream crashed: {e}")

    t = threading.Thread(target=run_stream, daemon=True)
    t.start()


@app.on_event("startup")
def on_startup() -> None:
    # Start websocket stream on boot
    start_sip_stream_thread()


# ---- ROUTES ----

@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "base_url": APCA_BASE_URL,
        "feed": ALPACA_DATA_FEED,
        "sip_symbols": SIP_SYMBOLS,
        "keys_present": bool(APCA_KEY and APCA_SECRET),
    }


@app.get("/latest_trade")
def latest_trade(symbol: str = Query("SPY")) -> Dict[str, Any]:
    """Return latest trade from websocket cache if present; otherwise fallback to REST latest trade."""
    sym = symbol.upper()

    with latest_trades_lock:
        cached = latest_trades.get(sym)

    if cached:
        return {"source": "websocket_cache", **cached}

    # Fallback to REST
    try:
        url = f"{APCA_BASE_URL}/v2/stocks/{sym}/trades/latest"
        params = {"feed": ALPACA_DATA_FEED}
        r = requests.get(url, headers=_alpaca_headers(), params=params, timeout=10)
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return {"source": "rest_latest_trade", **r.json()}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/bars")
def get_bars(
    symbol: str = Query("SPY"),
    timeframe: str = Query("5Min"),  # examples: 1Min, 5Min, 15Min, 1Hour, 1Day
    limit: int = Query(20, ge=1, le=1000),
) -> Dict[str, Any]:
    """
    OHLCV bars from Alpaca REST API.
    You do NOT enter params manually anywhere — GPT calls /bars with query params.
    """
    sym = symbol.upper()
    try:
        url = f"{APCA_BASE_URL}/v2/stocks/{sym}/bars"
        params = {
            "timeframe": timeframe,
            "limit": limit,
            "feed": ALPACA_DATA_FEED,  # sip vs iex
        }
        r = requests.get(url, headers=_alpaca_headers(), params=params, timeout=10)
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/daily")
def get_daily(
    symbol: str = Query("SPY"),
    limit: int = Query(50, ge=1, le=1000),
) -> Dict[str, Any]:
    """Convenience endpoint for daily bars."""
    return get_bars(symbol=symbol, timeframe="1Day", limit=limit)