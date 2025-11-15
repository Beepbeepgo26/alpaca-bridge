import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import requests

# Load .env file
load_dotenv()

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("APCA_BASE_URL", "https://data.alpaca.markets/v2")

if not ALPACA_KEY or not ALPACA_SECRET:
    raise RuntimeError("Alpaca API keys not set")

app = FastAPI(
    title="Alpaca Bridge API",
    description="Simple bridge between Alpaca and GPT Actions",
    version="1.0.0"
)

# Allow all origins (GPT Actions need this)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/bars")
def get_bars(symbol: str, timeframe: str = "5Min", limit: int = 50):
    """
    Get recent OHLCV bars for a symbol from Alpaca.
    Example:
      /bars?symbol=SPY&timeframe=5Min&limit=50
    """
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    url = f"{ALPACA_BASE_URL}/stocks/{symbol}/bars"
    params = {"timeframe": timeframe, "limit": limit}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))

    return r.json()