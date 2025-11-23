# main.py — RAW PRICE VERSION with STOCKS + FUTURES support

import os
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# ------------------------------------------------------------------
# Load environment variables from .env next to this file
# ------------------------------------------------------------------
BASE_DIR = os.path.dirname(__file__)
dotenv_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path)

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("APCA_BASE_URL", "https://data.alpaca.markets/v2")

if not ALPACA_KEY or not ALPACA_SECRET:
    raise RuntimeError("Alpaca API keys are not set in environment (.env)")

# Normalise base URL and derive root without `/v2`
if ALPACA_BASE_URL.endswith("/"):
    ALPACA_BASE_URL = ALPACA_BASE_URL.rstrip("/")

DATA_ROOT = ALPACA_BASE_URL
if DATA_ROOT.endswith("/v2"):
    DATA_ROOT = DATA_ROOT[:-3]  # strip the trailing "v2"

# ------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------
app = FastAPI(
    title="Alpaca Bridge API",
    description="Bridge between Alpaca market data and GPT Actions (RAW prices, stocks + futures).",
    version="1.0.0",
)

# CORS so GPT Actions can call this from anywhere
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"status": "ok", "message": "Alpaca Bridge is live (raw prices, stocks + futures)."}


# ------------------------------------------------------------------
# /bars endpoint — auto-detect STOCKS vs FUTURES
# ------------------------------------------------------------------
@app.get("/bars")
def get_bars(
    symbol: str,
    timeframe: str = "5Min",
    limit: int = 50,
):
    """
    Return RAW OHLCV bars from Alpaca.

    - For stocks/ETFs (SPY, AAPL, QQQ, etc.) → uses /v2/stocks/{symbol}/bars
    - For futures (ES, MES, NQ, MNQ, YM, etc.) → uses /v1beta3/markets/futures/us/{symbol}/bars

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

    # Symbols we treat as futures (raw, no “/MESmain” here – just the base symbol you’re using)
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
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Alpaca error: {e}")

    # We do NOT touch or rescale anything here.
    return r.json()