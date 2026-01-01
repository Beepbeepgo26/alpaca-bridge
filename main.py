import os
import threading
import time
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Alpaca streaming (alpaca-py)
from alpaca.data.live.stock import StockDataStream
from alpaca.data.enums import DataFeed


# ----------------------------
# ENV
# ----------------------------
load_dotenv()

ALPACA_KEY = os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET_KEY")

# REST base for market data (NOT trading)
ALPACA_DATA_BASE = os.getenv("ALPACA_DATA_BASE", "https://data.alpaca.markets")

# Default feed for market data (IEX or SIP)
DEFAULT_FEED = (os.getenv("ALPACA_DATA_FEED", "sip") or "sip").lower()

# Which symbols the server should keep “live” via websocket
SIP_SYMBOLS = [s.strip().upper() for s in (os.getenv("SIP_SYMBOLS", "SPY") or "SPY").split(",") if s.strip()]

if not ALPACA_KEY or not ALPACA_SECRET:
    raise RuntimeError("Missing Alpaca keys. Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env (or Render env vars).")


# ----------------------------
# APP
# ----------------------------
app = FastAPI(
    title="Alpaca Bridge API",
    description="HTTP bridge for Alpaca bars + server-side SIP websocket caching for GPT Actions.",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten later if you want
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------
# SIP cache (in-memory)
# ----------------------------
latest_trades: Dict[str, Dict[str, Any]] = {}
stream_status: Dict[str, Any] = {"running": False, "last_error": None, "last_update_utc": None, "feed": DEFAULT_FEED}


def _feed_enum(feed: str) -> DataFeed:
    f = (feed or "").lower()
    if f == "sip":
        return DataFeed.SIP
    return DataFeed.IEX


def start_sip_stream(feed: str = DEFAULT_FEED, symbols: Optional[list] = None) -> None:
    """
    Runs Alpaca websocket stream in a background thread and updates latest_trades.
    GPT will NOT connect to websocket; it will call /quote via HTTP.
    """
    symbols = symbols or SIP_SYMBOLS
    feed_enum = _feed_enum(feed)

    async def on_trade(trade):
        # alpaca-py trade has: trade.symbol, trade.price, trade.timestamp (and more)
        sym = str(trade.symbol).upper()
        latest_trades[sym] = {
            "symbol": sym,
            "price": float(trade.price),
            "timestamp": str(trade.timestamp),
        }
        stream_status["last_update_utc"] = time.time()

    def runner():
        try:
            stream_status["running"] = True
            stream_status["last_error"] = None
            stream_status["feed"] = "sip" if feed_enum == DataFeed.SIP else "iex"

            stream = StockDataStream(ALPACA_KEY, ALPACA_SECRET, feed=feed_enum)
            stream.subscribe_trades(on_trade, *symbols)
            stream.run()  # blocking
        except Exception as e:
            stream_status["running"] = False
            stream_status["last_error"] = repr(e)

    t = threading.Thread(target=runner, daemon=True)
    t.start()


@app.on_event("startup")
def _startup():
    # Start SIP (or IEX) stream in the background at boot
    start_sip_stream(DEFAULT_FEED, SIP_SYMBOLS)


# ----------------------------
# ROUTES
# ----------------------------
@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "Alpaca Bridge is live (bars + cached trades).",
        "default_feed": DEFAULT_FEED,
        "sip_symbols": SIP_SYMBOLS,
    }


@app.get("/stream_status")
def get_stream_status():
    return stream_status


@app.get("/quote")
def get_latest_trade(symbol: str = Query("SPY")):
    """
    Latest trade price from the websocket cache.
    """
    sym = symbol.upper()
    if sym not in latest_trades:
        raise HTTPException(
            status_code=404,
            detail=f"No trade cached yet for {sym}. (Is it subscribed? SIP_SYMBOLS={SIP_SYMBOLS})",
        )
    return latest_trades[sym]


@app.get("/bars")
def get_bars(
    symbol: str = Query("SPY"),
    timeframe: str = Query("5Min"),
    limit: int = Query(20, ge=1, le=10000),
    feed: Optional[str] = Query(None, description="iex or sip (stocks only). Default comes from ALPACA_DATA_FEED."),
):
    """
    Returns bars from Alpaca Market Data REST.

    Examples:
      /bars?symbol=SPY&timeframe=5Min&limit=20&feed=sip
      /bars?symbol=SPY&timeframe=1Day&limit=200&feed=sip
    """
    sym = symbol.upper()
    feed_to_use = (feed or DEFAULT_FEED).lower()

    # Alpaca bars endpoint for stocks/ETFs:
    # GET https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=5Min&limit=20&feed=sip
    url = f"{ALPACA_DATA_BASE}/v2/stocks/{sym}/bars"

    params = {
        "timeframe": timeframe,
        "limit": limit,
    }

    # Only stocks/ETFs support the feed param (iex/sip)
    if feed_to_use in ("iex", "sip"):
        params["feed"] = feed_to_use

    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=headers, params=params)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        # Show the response body for debugging
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=repr(e))