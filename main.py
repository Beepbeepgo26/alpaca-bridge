import os
import json
import asyncio
from datetime import datetime, timezone

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# ------------------------------------------------------------
# Load .env locally (Render env vars will override automatically)
# ------------------------------------------------------------
load_dotenv()

# ------------------------------------------------------------
# ENV VARS (accept a couple common key name variants)
# ------------------------------------------------------------
ALPACA_KEY = os.getenv("ALPACA_API_KEY_ID") or os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("ALPACA_API_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")

# IMPORTANT: Base should be https://data.alpaca.markets (no /v2)
ALPACA_BASE_URL = (os.getenv("ALPACA_BASE_URL") or "https://data.alpaca.markets").rstrip("/")
ALPACA_DATA_FEED = (os.getenv("ALPACA_DATA_FEED") or "iex").lower()

# SIP websocket
SIP_WS_URL = os.getenv("SIP_WS_URL") or "wss://stream.data.alpaca.markets/v2/sip"
SIP_SYMBOLS = os.getenv("SIP_SYMBOLS") or "SPY"
SIP_SYMBOL_LIST = [s.strip().upper() for s in SIP_SYMBOLS.split(",") if s.strip()]

# ------------------------------------------------------------
# FastAPI
# ------------------------------------------------------------
app = FastAPI(
    title="Alpaca Bridge API",
    description="Bridge between Alpaca Market Data (REST bars + SIP websocket) and GPT Actions.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # OK for prototype; restrict later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------
# In-memory store for latest SIP ticks (trades + quotes)
# latest[symbol] = { ... }
# ------------------------------------------------------------
latest = {}
latest_lock = asyncio.Lock()
sip_task = None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


async def sip_stream_loop():
    """
    Connects to Alpaca SIP websocket and keeps latest trades/quotes in memory.
    Reconnects forever if connection drops.
    """
    if not ALPACA_KEY or not ALPACA_SECRET:
        print("[SIP] Missing ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY. SIP stream will not start.")
        return

    symbols = SIP_SYMBOL_LIST or ["SPY"]

    while True:
        try:
            print(f"[SIP] Connecting to {SIP_WS_URL} symbols={symbols} ...")

            async with websockets.connect(SIP_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                # 1) auth
                await ws.send(json.dumps({
                    "action": "auth",
                    "key": ALPACA_KEY,
                    "secret": ALPACA_SECRET
                }))

                auth_msg = await ws.recv()
                print("[SIP] Auth response:", auth_msg)

                # 2) subscribe (trades + quotes)
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "trades": symbols,
                    "quotes": symbols
                }))

                sub_msg = await ws.recv()
                print("[SIP] Subscribe response:", sub_msg)

                # 3) read forever
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        if not isinstance(data, list):
                            continue

                        async with latest_lock:
                            for msg in data:
                                t = msg.get("T")  # message type
                                sym = (msg.get("S") or "").upper()
                                if not sym:
                                    continue

                                # Trade message example: {"T":"t","S":"SPY","p":680.12,"t":"..."}
                                if t == "t":
                                    latest.setdefault(sym, {})
                                    latest[sym]["last_trade_price"] = msg.get("p")
                                    latest[sym]["last_trade_time"] = msg.get("t")
                                    latest[sym]["updated_at"] = _now_iso()

                                # Quote message example: {"T":"q","S":"SPY","bp":..,"ap":..,"t":"..."}
                                elif t == "q":
                                    latest.setdefault(sym, {})
                                    latest[sym]["bid_price"] = msg.get("bp")
                                    latest[sym]["ask_price"] = msg.get("ap")
                                    latest[sym]["quote_time"] = msg.get("t")
                                    latest[sym]["updated_at"] = _now_iso()

                    except Exception:
                        # ignore malformed message
                        pass

        except Exception as e:
            print(f"[SIP] Disconnected/error: {e}. Reconnecting in 3s...")
            await asyncio.sleep(3)


@app.on_event("startup")
async def _startup():
    global sip_task
    # Start SIP background task (non-blocking)
    sip_task = asyncio.create_task(sip_stream_loop())


# ------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------
@app.get("/")
def root():
    return {
        "status": "ok",
        "message": "Alpaca Bridge is live (REST bars + SIP websocket).",
        "have_keys": bool(ALPACA_KEY and ALPACA_SECRET),
        "rest_feed": ALPACA_DATA_FEED,
        "sip_symbols": SIP_SYMBOL_LIST,
        "sip_ws_url": SIP_WS_URL,
    }


@app.get("/health")
def health():
    return root()


@app.get("/latest")
async def get_latest(symbol: str = Query("SPY")):
    sym = symbol.upper()
    async with latest_lock:
        out = latest.get(sym)

    return {
        "symbol": sym,
        "latest": out,
        "note": "If latest is null, SIP stream is not running or no messages received yet."
    }


@app.get("/bars")
async def get_bars(
    symbol: str = Query(..., description="Stock/ETF symbol, e.g. SPY"),
    timeframe: str = Query("5Min", description="Examples: 1Min, 5Min, 15Min, 1Hour, 1Day"),
    limit: int = Query(50, ge=1, le=10000),
):
    """
    Returns RAW bars from Alpaca REST.
    Supports BOTH minute bars and daily bars by choosing timeframe (e.g. 5Min vs 1Day).
    Uses ALPACA_DATA_FEED (sip/iex) automatically.
    """
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise HTTPException(status_code=500, detail="Missing Alpaca API keys in environment variables.")

    sym = symbol.upper()

    url = f"{ALPACA_BASE_URL}/v2/stocks/{sym}/bars"

    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }

    # THIS is where params go — in code — not Render.
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "feed": ALPACA_DATA_FEED,   # sip or iex
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=headers, params=params)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
