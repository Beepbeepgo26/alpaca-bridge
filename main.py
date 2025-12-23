# main.py – RAW PRICE VERSION with STOCKS + FUTURES support + SIP streaming

import os
import requests
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from alpaca.data.live import StockDataStream

# ---------------------------------------------------------------------------
# Load environment variables from .env next to this file
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(__file__)
dotenv_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path)

ALPACA_KEY = os.getenv("ALPACA_API_KEY_ID")
ALPACA_SECRET = os.getenv("ALPACA_API_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://data.alpaca.markets/v2")

if not ALPACA_KEY or not ALPACA_SECRET:
    raise RuntimeError("Alpaca API keys are not set in environment (.env)")

# Normalise base URL and derive root without '/v2'
if ALPACA_BASE_URL.endswith("/"):
    ALPACA_BASE_URL = ALPACA_BASE_URL.rstrip("/")

DATA_ROOT = ALPACA_BASE_URL
if DATA_ROOT.endswith("/v2"):
    DATA_ROOT = DATA_ROOT[:-3]  # strip the trailing "v2"

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Alpaca Bridge API",
    description="Bridge between Alpaca market data and GPT Actions (RAW prices, stocks + futures).",
    version="1.0.0",
)

# In-memory store for latest SIP quotes
# e.g. latest_quotes = {"SPY": {"price": 680.12, "timestamp": "..."}}
latest_quotes: dict = {}

# CORS so GPT Actions can call this from anywhere
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# SIP websocket: keep latest trades in memory
# ---------------------------------------------------------------------------

async def run_sip_stream():
    """
    Connect to Alpaca SIP websocket and keep updating latest_quotes
    with the most recent trades.
    """
    if not ALPACA_KEY or not ALPACA_SECRET:
        print("Alpaca API keys not set; SIP stream will not start.")
        return

    # Create the stock data stream for SIP
    stream = StockDataStream(ALPACA_KEY, ALPACA_SECRET)

    async def on_trade(trade):
        symbol = trade.symbol.upper()
        latest_quotes[symbol] = {
            "price": float(trade.price),
            "timestamp": str(trade.timestamp),
        }

    # Subscribe to SPY trades for now (you can add more symbols later)
    stream.subscribe_trades(on_trade, "SPY")

    # Run the stream forever
    await stream.run()


@app.on_event("startup")
async def startup_event():
    # Start SIP websocket in the background when the app boots
    asyncio.create_task(run_sip_stream())

# ---------------------------------------------------------------------------
# Health / root
# ---------------------------------------------------------------------------

@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Alpaca Bridge is live (raw prices, stocks + futures).",
    }

# ---------------------------------------------------------------------------
# /bars endpoint – auto-detect STOCKS vs FUTURES
# ---------------------------------------------------------------------------

import httpx
from fastapi import Query

@app.get("/bars")
async def get_bars(
    symbol: str,
    timeframe: str = "5Min",
    limit: int = Query(20, ge=1, le=1000, description="Number of bars to return (1-1000)"),
):
    """
    Return RAW OHLCV bars from Alpaca.

    - For stocks/ETFs (SPY, AAPL, QQQ, etc.) -> uses /v2/stocks/{symbol}/bars
    - For futures (ES, MES, NQ, MNQ, YM, MYM, RTY, M2K, etc.) -> uses
      /v1beta3/markets/futures/us/{symbol}/bars

    Example:

        /bars?symbol=SPY&timeframe=5Min&limit=50
        /bars?symbol=ES&timeframe=5Min&limit=50

    NOTE: This endpoint does NOT scale, normalize, or transform prices.
    Whatever Alpaca sends (o, h, l, c, v) is returned directly.
    """

    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }

    # Symbols we treat as futures (raw, no "/MESmain" here – just the base symbol you're using)
    future_symbols = {"ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K"}

    sym_upper = symbol.upper()

    if sym_upper in future_symbols:
        # FUTURES endpoint
        url = f"{DATA_ROOT}/v1beta3/markets/futures/us/{sym_upper}/bars"
    else:
        # STOCKS/ETFs endpoint
        url = f"{DATA_ROOT}/v2/stocks/{symbol}/bars"

    params = {
        "timeframe": timeframe,
        "limit": limit,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers, params=params)
            r.raise_for_status()
    except httpx.RequestError as e:
        raise HTTPException(status_code=500, detail=f"Alpaca error: {e}")

    # We do NOT touch or rescale anything here.
    return r.json()

# ---------------------------------------------------------------------------
# /last_quote endpoint – latest SIP trade from websocket
# ---------------------------------------------------------------------------

@app.get("/last_quote")
async def last_quote(symbol: str = "SPY"):
    """
    Returns the latest SIP quote for the given symbol from the in-memory store.
    """
    symbol = symbol.upper()
    if symbol not in latest_quotes:
        raise HTTPException(status_code=404, detail=f"No quote received yet for {symbol}.")
    return {
        "symbol": symbol,
        "price": latest_quotes[symbol]["price"],
        "timestamp": latest_quotes[symbol]["timestamp"],
    }