from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from datetime import datetime, timedelta
import requests
import os

# ─────────────────────────────────────────
# Load environment variables from .env
# ─────────────────────────────────────────
load_dotenv()

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("APCA_BASE_URL", "https://data.alpaca.markets/v2")

if not ALPACA_KEY or not ALPACA_SECRET:
    raise RuntimeError("Alpaca API keys not set. Check APCA_API_KEY_ID and APCA_API_SECRET_KEY in your environment.")

# ─────────────────────────────────────────
# FastAPI app setup
# ─────────────────────────────────────────
app = FastAPI(
    title="Alpaca Bridge API",
    description="Simple bridge between Alpaca and GPT Actions",
    version="1.0.0",
)

# Allow all origins so GPT Actions can call this
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────
# Health check / root endpoint
# ─────────────────────────────────────────
@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "Alpaca Bridge is running",
        "docs": "/docs",
    }

# ─────────────────────────────────────────
# /bars endpoint: fetch recent OHLCV bars
# ─────────────────────────────────────────
@app.get("/bars")
def get_bars(symbol: str, timeframe: str = "5Min", limit: int = 50):
    """
    Get recent OHLCV bars for a symbol from Alpaca.

    Example:
        /bars?symbol=SPY&timeframe=5Min&limit=50

    Alpaca's /bars endpoint requires a 'start' parameter, so we
    automatically request data starting 2 days ago in UTC.
    """
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }

    # Alpaca requires an ISO8601 UTC timestamp, e.g. 2024-11-15T00:00:00Z
    start_time = (datetime.utcnow() - timedelta(days=2)).isoformat(timespec="seconds") + "Z"

    url = f"{ALPACA_BASE_URL}/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit": limit,
        "start": start_time,
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        # Bubble up as a 500 error to the client (GPT)
        raise HTTPException(status_code=500, detail=str(e))

    return response.json()