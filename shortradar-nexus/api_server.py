#!/usr/bin/env python3
"""
ShortRadar — Enhanced FastAPI Backend
Multi-source data aggregation with WebSocket streaming.
Sources: yfinance (default), Alpha Vantage, Finnhub, Bookmap/dxFeed, IBKR
"""
import asyncio
import json
import time
import os
import random
import math
import traceback
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from typing import Optional
from enum import Enum

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field
import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "demo")
FINNHUB_KEY = os.getenv("FINNHUB_KEY", "demo")
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "5000"))
BOOKMAP_WS = os.getenv("BOOKMAP_WS", "")
DATABENTO_KEY = os.getenv("DATABENTO_KEY", "")
# Databento dataset config
# EQUS.MINI = consolidated US equities live (cost-effective, top-of-book)
# DBEQ.BASIC = consolidated US equities historical (richer data)
DATABENTO_LIVE_DATASET = os.getenv("DATABENTO_LIVE_DATASET", "EQUS.MINI")
DATABENTO_HIST_DATASET = os.getenv("DATABENTO_HIST_DATASET", "DBEQ.BASIC")

DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOG", "META", "SPY", "QQQ", "AMD"]

# Company name mapping
COMPANY_NAMES = {
    "MSTR": "MicroStrategy Inc", "COIN": "Coinbase Global", "SMCI": "Super Micro Computer",
    "PLTR": "Palantir Technologies", "ARM": "Arm Holdings", "MARA": "Marathon Digital",
    "RIOT": "Riot Platforms", "CLSK": "CleanSpark Inc", "UPST": "Upstart Holdings",
    "AFRM": "Affirm Holdings", "HOOD": "Robinhood Markets", "SOFI": "SoFi Technologies",
    "LCID": "Lucid Group", "RIVN": "Rivian Automotive", "IONQ": "IonQ Inc",
    "RGTI": "Rigetti Computing", "SOUN": "SoundHound AI", "RKLB": "Rocket Lab USA",
    "LUNR": "Intuitive Machines", "JOBY": "Joby Aviation", "ROKU": "Roku Inc",
    "AAPL": "Apple Inc", "MSFT": "Microsoft Corp", "NVDA": "NVIDIA Corp",
    "TSLA": "Tesla Inc", "AMZN": "Amazon.com Inc", "GOOG": "Alphabet Inc",
    "META": "Meta Platforms", "SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco QQQ Trust",
    "AMD": "Advanced Micro Devices",
}

# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------
class DataSource(str, Enum):
    YFINANCE = "yfinance"
    ALPHA_VANTAGE = "alpha_vantage"
    FINNHUB = "finnhub"
    BOOKMAP = "bookmap"
    IBKR = "ibkr"
    DATABENTO = "databento"

class SourceStatus(BaseModel):
    source: str
    connected: bool
    last_update: Optional[str] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None

class Quote(BaseModel):
    symbol: str
    price: float
    change: float = 0
    change_pct: float = 0
    volume: int = 0
    high: float = 0
    low: float = 0
    open: float = 0
    prev_close: float = 0
    bid: float = 0
    ask: float = 0
    bid_size: int = 0
    ask_size: int = 0
    timestamp: str = ""
    source: str = "yfinance"

class TechnicalIndicators(BaseModel):
    symbol: str
    rsi_14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    ema_12: Optional[float] = None
    ema_26: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_middle: Optional[float] = None
    atr_14: Optional[float] = None
    vwap: Optional[float] = None

class ShortCandidate(BaseModel):
    symbol: str
    company_name: str = ""
    score: int = 0
    price: float = 0
    change_pct: float = 0
    volume: int = 0
    rsi: Optional[float] = None
    rsi_status: str = "Neutral"
    macd_hist: Optional[float] = None
    macd_value: Optional[float] = None
    macd_direction: str = "Neutral"
    dist_sma50: Optional[float] = None
    dist_sma200: Optional[float] = None
    sma50_price: Optional[float] = None
    sma200_price: Optional[float] = None
    sma50_relation: str = "N/A"
    sma200_relation: str = "N/A"
    sharpe_ratio: Optional[float] = None
    sharpe_label: str = "N/A"
    volatility_pct: Optional[float] = None
    spread_pct: Optional[float] = None
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    bid_size: int = 0
    ask_size: int = 0
    market_cap: Optional[str] = None
    mentions: int = 0
    sentiment: str = "Mixed"
    book_imbalance_pct: float = 0
    book_status: str = "Balanced"
    risk_level: str = "low"

class CandleBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int

class NASDAQIndex(BaseModel):
    price: float
    change: float
    change_pct: float
    timestamp: str

# ---------------------------------------------------------------------------
# Persistent storage helpers
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")

def _load_watchlist() -> list[str]:
    """Load watchlist from disk, falling back to defaults."""
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    print(f"[Persist] Loaded {len(data)} symbols from {WATCHLIST_FILE}")
                    return data
    except Exception as e:
        print(f"[Persist] Failed to load watchlist: {e}")
    return list(DEFAULT_WATCHLIST)

def _save_watchlist(symbols: list[str]):
    """Save watchlist to disk."""
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(symbols, f, indent=2)
    except Exception as e:
        print(f"[Persist] Failed to save watchlist: {e}")


# ---------------------------------------------------------------------------
# In-memory data store (cache)
# ---------------------------------------------------------------------------
class DataStore:
    def __init__(self):
        self.quotes: dict[str, Quote] = {}
        self.technicals: dict[str, TechnicalIndicators] = {}
        self.candles: dict[str, list[CandleBar]] = {}
        self.short_candidates: list[ShortCandidate] = []
        self.source_status: dict[str, SourceStatus] = {}
        self.watchlist: list[str] = _load_watchlist()
        self.nasdaq_index: Optional[NASDAQIndex] = None
        self._lock = asyncio.Lock()

    def persist_watchlist(self):
        """Save current watchlist to disk."""
        _save_watchlist(self.watchlist)

    async def update_quote(self, quote: Quote):
        async with self._lock:
            self.quotes[quote.symbol] = quote

    async def update_technicals(self, tech: TechnicalIndicators):
        async with self._lock:
            self.technicals[tech.symbol] = tech

    async def update_source_status(self, status: SourceStatus):
        async with self._lock:
            self.source_status[status.source] = status

store = DataStore()

# ---------------------------------------------------------------------------
# Data Source Adapters
# ---------------------------------------------------------------------------
async def fetch_yfinance_quotes(symbols: list[str]) -> list[Quote]:
    """Fetch quotes via yfinance (runs in thread since it's synchronous)."""
    import yfinance as yf

    def _fetch():
        results = []
        try:
            tickers = yf.Tickers(" ".join(symbols))
            for sym in symbols:
                try:
                    t = tickers.tickers.get(sym)
                    if not t:
                        continue
                    info = t.fast_info
                    price = float(info.last_price) if hasattr(info, 'last_price') and info.last_price else 0
                    prev = float(info.previous_close) if hasattr(info, 'previous_close') and info.previous_close else price
                    change = price - prev
                    change_pct = (change / prev * 100) if prev else 0
                    vol = int(info.last_volume) if hasattr(info, 'last_volume') and info.last_volume else 0

                    q = Quote(
                        symbol=sym,
                        price=round(price, 2),
                        change=round(change, 2),
                        change_pct=round(change_pct, 2),
                        volume=vol,
                        high=round(float(info.day_high), 2) if hasattr(info, 'day_high') and info.day_high else price,
                        low=round(float(info.day_low), 2) if hasattr(info, 'day_low') and info.day_low else price,
                        open=round(float(info.open), 2) if hasattr(info, 'open') and info.open else price,
                        prev_close=round(prev, 2),
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        source="yfinance"
                    )
                    results.append(q)
                except Exception:
                    pass
        except Exception:
            pass
        return results

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch)


async def fetch_yfinance_technicals(symbol: str) -> Optional[TechnicalIndicators]:
    """Calculate technicals from yfinance historical data."""
    import yfinance as yf
    import numpy as np

    def _calc():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="6mo", interval="1d")
            if hist.empty or len(hist) < 26:
                return None

            close = hist['Close'].values
            volume = hist['Volume'].values

            # RSI 14
            deltas = np.diff(close)
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            avg_gain = np.mean(gains[-14:])
            avg_loss = np.mean(losses[-14:])
            rs = avg_gain / avg_loss if avg_loss != 0 else 100
            rsi = 100 - (100 / (1 + rs))

            # MACD
            ema12 = _ema(close, 12)
            ema26 = _ema(close, 26)
            macd_line = ema12[-1] - ema26[-1]
            macd_series = [_ema(close[:i+1], 12)[-1] - _ema(close[:i+1], 26)[-1]
                           for i in range(25, len(close))]
            signal = _ema(macd_series, 9)[-1] if len(macd_series) >= 9 else 0
            macd_hist = macd_line - signal

            # SMAs
            sma20 = float(np.mean(close[-20:])) if len(close) >= 20 else None
            sma50 = float(np.mean(close[-50:])) if len(close) >= 50 else None
            sma200 = float(np.mean(close[-200:])) if len(close) >= 200 else None

            # Bollinger Bands
            if sma20:
                std20 = float(np.std(close[-20:]))
                bb_upper = sma20 + 2 * std20
                bb_lower = sma20 - 2 * std20
            else:
                bb_upper = bb_lower = None

            # ATR 14
            if len(hist) >= 15:
                highs = hist['High'].values[-15:]
                lows = hist['Low'].values[-15:]
                closes = close[-15:]
                trs = []
                for i in range(1, len(highs)):
                    tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                    trs.append(tr)
                atr = float(np.mean(trs[-14:]))
            else:
                atr = None

            # VWAP (intraday approx)
            if len(volume) > 0 and np.sum(volume[-20:]) > 0:
                tp = (hist['High'].values[-20:] + hist['Low'].values[-20:] + close[-20:]) / 3
                vwap = float(np.sum(tp * volume[-20:]) / np.sum(volume[-20:]))
            else:
                vwap = None

            return TechnicalIndicators(
                symbol=symbol,
                rsi_14=round(rsi, 2),
                macd=round(macd_line, 4),
                macd_signal=round(signal, 4),
                macd_hist=round(macd_hist, 4),
                sma_20=round(sma20, 2) if sma20 else None,
                sma_50=round(sma50, 2) if sma50 else None,
                sma_200=round(sma200, 2) if sma200 else None,
                ema_12=round(float(ema12[-1]), 2),
                ema_26=round(float(ema26[-1]), 2),
                bb_upper=round(bb_upper, 2) if bb_upper else None,
                bb_lower=round(bb_lower, 2) if bb_lower else None,
                bb_middle=round(sma20, 2) if sma20 else None,
                atr_14=round(atr, 2) if atr else None,
                vwap=round(vwap, 2) if vwap else None,
            )
        except Exception:
            return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _calc)


def _ema(data, period):
    """Exponential moving average."""
    import numpy as np
    if len(data) < period:
        return data
    ema = [float(np.mean(data[:period]))]
    mult = 2 / (period + 1)
    for val in data[period:]:
        ema.append(float(val) * mult + ema[-1] * (1 - mult))
    return ema


async def fetch_alpha_vantage_quote(symbol: str) -> Optional[Quote]:
    """Fetch from Alpha Vantage Global Quote."""
    if ALPHA_VANTAGE_KEY == "demo":
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://www.alphavantage.co/query",
                params={"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": ALPHA_VANTAGE_KEY}
            )
            data = r.json().get("Global Quote", {})
            if not data:
                return None
            price = float(data.get("05. price", 0))
            return Quote(
                symbol=symbol,
                price=price,
                change=float(data.get("09. change", 0)),
                change_pct=float(data.get("10. change percent", "0").replace("%", "")),
                volume=int(data.get("06. volume", 0)),
                high=float(data.get("03. high", 0)),
                low=float(data.get("04. low", 0)),
                open=float(data.get("02. open", 0)),
                prev_close=float(data.get("08. previous close", 0)),
                timestamp=datetime.now(timezone.utc).isoformat(),
                source="alpha_vantage"
            )
    except Exception:
        return None


async def fetch_finnhub_quote(symbol: str) -> Optional[Quote]:
    """Fetch from Finnhub quote endpoint."""
    if FINNHUB_KEY == "demo":
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": symbol, "token": FINNHUB_KEY}
            )
            d = r.json()
            if not d or d.get("c", 0) == 0:
                return None
            return Quote(
                symbol=symbol,
                price=round(d["c"], 2),
                change=round(d["d"], 2) if d.get("d") else 0,
                change_pct=round(d["dp"], 2) if d.get("dp") else 0,
                high=round(d.get("h", 0), 2),
                low=round(d.get("l", 0), 2),
                open=round(d.get("o", 0), 2),
                prev_close=round(d.get("pc", 0), 2),
                timestamp=datetime.now(timezone.utc).isoformat(),
                source="finnhub"
            )
    except Exception:
        return None


async def fetch_finnhub_sentiment(symbol: str) -> dict:
    """Fetch social sentiment from Finnhub."""
    if FINNHUB_KEY == "demo":
        return {"mentions": 0, "sentiment": "neutral", "score": 0}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://finnhub.io/api/v1/stock/social-sentiment",
                params={"symbol": symbol, "token": FINNHUB_KEY}
            )
            d = r.json()
            reddit = d.get("reddit", [])
            twitter = d.get("twitter", [])
            mentions = sum(x.get("mention", 0) for x in reddit + twitter)
            pos = sum(x.get("positiveScore", 0) for x in reddit + twitter)
            neg = sum(x.get("negativeScore", 0) for x in reddit + twitter)
            total = pos + neg
            if total > 0:
                score = (pos - neg) / total
                sentiment = "bullish" if score > 0.2 else "bearish" if score < -0.2 else "mixed"
            else:
                score = 0
                sentiment = "neutral"
            return {"mentions": mentions, "sentiment": sentiment, "score": round(score, 2)}
    except Exception:
        return {"mentions": 0, "sentiment": "neutral", "score": 0}


async def fetch_yfinance_candles(symbol: str, period: str = "5d", interval: str = "5m") -> list[CandleBar]:
    """Fetch OHLCV candles from yfinance."""
    import yfinance as yf

    def _fetch():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period=period, interval=interval)
            bars = []
            for idx, row in hist.iterrows():
                bars.append(CandleBar(
                    timestamp=idx.isoformat(),
                    open=round(float(row['Open']), 2),
                    high=round(float(row['High']), 2),
                    low=round(float(row['Low']), 2),
                    close=round(float(row['Close']), 2),
                    volume=int(row['Volume'])
                ))
            return bars
        except Exception:
            return []

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch)


async def fetch_nasdaq_index() -> Optional[NASDAQIndex]:
    """Fetch NASDAQ Composite index data."""
    import yfinance as yf

    def _fetch():
        try:
            t = yf.Ticker("^IXIC")
            info = t.fast_info
            price = float(info.last_price) if hasattr(info, 'last_price') and info.last_price else 0
            prev = float(info.previous_close) if hasattr(info, 'previous_close') and info.previous_close else price
            change = price - prev
            change_pct = (change / prev * 100) if prev else 0
            return NASDAQIndex(
                price=round(price, 2),
                change=round(change, 2),
                change_pct=round(change_pct, 2),
                timestamp=datetime.now(timezone.utc).isoformat()
            )
        except Exception:
            return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch)


# ---------------------------------------------------------------------------
# Databento Adapter (MBP-1 — real-time best bid/offer)
# ---------------------------------------------------------------------------
# State for Databento live stream
_databento_live_client = None
_databento_quotes: dict[str, dict] = {}  # symbol -> {bid, ask, bid_sz, ask_sz, last, ts}
_databento_connected = False
_databento_last_update: Optional[str] = None
_databento_error: Optional[str] = None
_databento_record_count = 0  # debug counter
_databento_symbol_map: dict[int, str] = {}  # instrument_id -> symbol


async def databento_start_live_stream(symbols: list[str]):
    """Start a Databento live streaming session using mbp-1 schema.
    MBP-1 provides best bid/offer (BBO) updates on every change.
    Runs in a daemon background thread since the Databento client is synchronous."""
    global _databento_live_client, _databento_connected, _databento_last_update, _databento_error

    if not DATABENTO_KEY:
        _databento_error = "No API key (set DATABENTO_KEY)"
        return

    import threading

    def _run_stream():
        global _databento_live_client, _databento_connected, _databento_last_update, _databento_error, _databento_record_count
        try:
            import databento as db

            print(f"[Databento] Connecting to {DATABENTO_LIVE_DATASET} mbp-1 for {len(symbols)} symbols...", flush=True)
            _databento_live_client = db.Live(key=DATABENTO_KEY)

            # Subscribe to mbp-1 schema (Market By Price, Level 1 = top of book)
            # This gives us real-time best bid/offer on every BBO change
            _databento_live_client.subscribe(
                dataset=DATABENTO_LIVE_DATASET,
                schema="mbp-1",
                stype_in="raw_symbol",
                symbols=symbols,
            )

            _databento_connected = True
            _databento_error = None
            _databento_last_update = datetime.now(timezone.utc).isoformat()
            print(f"[Databento] Live mbp-1 stream started on {DATABENTO_LIVE_DATASET}", flush=True)

            # Process incoming MBP-1 records via callback
            def handle_record(record):
                global _databento_last_update, _databento_record_count
                try:
                    import databento as db

                    # Build symbol map from SymbolMappingMsg
                    if isinstance(record, db.SymbolMappingMsg):
                        iid = record.instrument_id
                        sym_in = record.stype_in_symbol.strip('\x00').strip()
                        if sym_in:
                            _databento_symbol_map[iid] = sym_in
                            print(f"[Databento] Mapped instrument {iid} -> {sym_in}", flush=True)
                        return

                    # Handle MBP1Msg — provides best bid/offer
                    if isinstance(record, db.MBP1Msg):
                        iid = record.instrument_id
                        sym = _databento_symbol_map.get(iid)
                        if not sym:
                            return  # Skip if we don't know the symbol yet

                        # Extract bid/ask from level 0 (top of book)
                        level = record.levels[0]
                        bid_px = float(level.bid_px) / 1e9  # fixed-point to dollars
                        ask_px = float(level.ask_px) / 1e9
                        bid_sz = int(level.bid_sz)
                        ask_sz = int(level.ask_sz)

                        # Mid price as "last" when no trade price available
                        mid = round((bid_px + ask_px) / 2, 4) if bid_px > 0 and ask_px > 0 else 0
                        # Use record.price for trade price if available (> 0)
                        trade_px = float(record.price) / 1e9 if record.price and record.price > 0 else 0
                        last = trade_px if trade_px > 0 else mid

                        ts = datetime.now(timezone.utc).isoformat()

                        _databento_quotes[sym] = {
                            "symbol": sym,
                            "bid": round(bid_px, 4),
                            "ask": round(ask_px, 4),
                            "bid_sz": bid_sz,
                            "ask_sz": ask_sz,
                            "last": round(last, 4),
                            "mid": round(mid, 4),
                            "timestamp": ts,
                        }
                        _databento_last_update = ts
                        _databento_record_count += 1

                        # Log first few records for debugging
                        if _databento_record_count <= 5:
                            print(f"[Databento] MBP1 #{_databento_record_count}: {sym} bid={bid_px:.2f}x{bid_sz} ask={ask_px:.2f}x{ask_sz} last={last:.2f}", flush=True)
                        elif _databento_record_count == 50:
                            print(f"[Databento] 50 records received, {len(_databento_quotes)} symbols active", flush=True)
                        elif _databento_record_count % 500 == 0:
                            print(f"[Databento] {_databento_record_count} records, {len(_databento_quotes)} symbols", flush=True)

                except Exception as e:
                    print(f"[Databento] Record handler error: {e}", flush=True)

            _databento_live_client.add_callback(handle_record)
            _databento_live_client.start()
            _databento_live_client.block_for_close()

        except Exception as e:
            _databento_connected = False
            _databento_error = str(e)
            print(f"[Databento] Stream error: {e}", flush=True)
            traceback.print_exc()

    # Launch as a daemon thread so it doesn't block server startup or shutdown
    t = threading.Thread(target=_run_stream, daemon=True, name="databento-live")
    t.start()
    print(f"[Databento] Background thread launched", flush=True)


async def databento_stop_live_stream():
    """Gracefully stop the Databento live stream."""
    global _databento_live_client, _databento_connected
    if _databento_live_client:
        try:
            _databento_live_client.stop()
        except Exception:
            try:
                _databento_live_client.terminate()
            except Exception:
                pass
    _databento_connected = False
    _databento_live_client = None


async def fetch_databento_historical_quotes(symbols: list[str]) -> list[Quote]:
    """Fetch latest trades from Databento Historical API as a fallback.
    Uses ohlcv-1m schema for the most recent minute bars."""
    if not DATABENTO_KEY:
        return []

    def _fetch():
        results = []
        try:
            import databento as db
            client = db.Historical(key=DATABENTO_KEY)
            import pandas as pd

            # Use end=today (midnight UTC) to stay within available range
            end_date = pd.Timestamp.now(tz="UTC").normalize()  # midnight today
            start_date = end_date - pd.Timedelta(days=5)

            # Get last available OHLCV-1d bars for each symbol
            data = client.timeseries.get_range(
                dataset=DATABENTO_HIST_DATASET,
                schema="ohlcv-1d",
                symbols=symbols,
                stype_in="raw_symbol",
                start=start_date,
                end=end_date,
            )
            df = data.to_df()
            if df.empty:
                return results

            # Group by symbol and take the latest row per symbol
            if 'symbol' in df.columns:
                grouped = df.groupby('symbol')
            else:
                # symbol might be in the index
                df_reset = df.reset_index()
                if 'symbol' in df_reset.columns:
                    grouped = df_reset.groupby('symbol')
                else:
                    return results

            for sym, sym_df in grouped:
                if sym_df.empty:
                    continue
                row = sym_df.iloc[-1]
                price = float(row['close'])
                open_p = float(row['open'])
                high = float(row['high'])
                low = float(row['low'])
                vol = int(row['volume'])
                change = price - open_p
                change_pct = (change / open_p * 100) if open_p > 0 else 0

                # Only include symbols that were requested
                clean_sym = str(sym).strip()
                if clean_sym not in symbols:
                    continue

                results.append(Quote(
                    symbol=clean_sym,
                    price=round(price, 2),
                    change=round(change, 2),
                    change_pct=round(change_pct, 2),
                    volume=vol,
                    high=round(high, 2),
                    low=round(low, 2),
                    open=round(open_p, 2),
                    prev_close=round(open_p, 2),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    source="databento"
                ))
        except Exception as e:
            print(f"[Databento] Historical fetch error: {e}")
            traceback.print_exc()
        return results

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch)


def _get_databento_quotes_from_live() -> list[Quote]:
    """Convert live mbp-1 cache into Quote objects with real bid/ask."""
    quotes = []
    for sym, data in _databento_quotes.items():
        # Map raw symbols to our watchlist format (strip exchange suffixes if any)
        clean_sym = sym.split('.')[0] if '.' in sym else sym

        bid = data.get("bid", 0)
        ask = data.get("ask", 0)
        last = data.get("last", 0)
        mid = data.get("mid", 0)
        # Use last trade price if available, otherwise mid
        price = last if last > 0 else mid

        # Calculate spread
        spread_pct = 0
        if bid > 0 and ask > 0 and ask > bid:
            spread_pct = round((ask - bid) / mid * 100, 4) if mid > 0 else 0

        quotes.append(Quote(
            symbol=clean_sym,
            price=round(price, 2),
            change=0,  # Need prev_close context to calculate
            change_pct=0,
            bid=round(bid, 2),
            ask=round(ask, 2),
            bid_size=data.get("bid_sz", 0),
            ask_size=data.get("ask_sz", 0),
            timestamp=data["timestamp"],
            source="databento"
        ))
    return quotes


def _format_market_cap(cap_value):
    """Format market cap into human readable string."""
    if cap_value is None or cap_value == 0:
        return "N/A"
    if cap_value >= 1e12:
        return f"${cap_value/1e12:.2f}T"
    if cap_value >= 1e9:
        return f"${cap_value/1e9:.2f}B"
    if cap_value >= 1e6:
        return f"${cap_value/1e6:.2f}M"
    return f"${cap_value:,.0f}"

def _format_volume(vol):
    """Format volume into human readable string."""
    if vol >= 1e9:
        return f"{vol/1e9:.1f}B"
    if vol >= 1e6:
        return f"{vol/1e6:.1f}M"
    if vol >= 1e3:
        return f"{vol/1e3:.1f}K"
    return str(vol)


# ---------------------------------------------------------------------------
# Enhanced Short Scanner Logic
# ---------------------------------------------------------------------------
async def scan_short_candidates() -> list[ShortCandidate]:
    """Scan top gainers for short opportunities with enhanced metrics."""
    import yfinance as yf
    import numpy as np

    # Merge hardcoded scanner symbols with user-added watchlist symbols
    _base_scanner = [
        "MSTR", "COIN", "SMCI", "PLTR", "ARM", "MARA", "RIOT", "CLSK",
        "UPST", "AFRM", "HOOD", "SOFI", "LCID", "RIVN", "IONQ", "RGTI",
        "SOUN", "RKLB", "LUNR", "JOBY", "ROKU"
    ]
    all_scanner_symbols = list(dict.fromkeys(_base_scanner + store.watchlist))  # dedupe, preserve order

    def _scan():
        candidates = []
        try:
            screener_symbols = all_scanner_symbols
            tickers = yf.Tickers(" ".join(screener_symbols))
            for sym in screener_symbols:
                try:
                    t = tickers.tickers.get(sym)
                    if not t:
                        continue
                    info = t.fast_info
                    price = float(info.last_price) if hasattr(info, 'last_price') and info.last_price else 0
                    prev = float(info.previous_close) if hasattr(info, 'previous_close') and info.previous_close else price
                    if prev == 0:
                        continue
                    change_pct = (price - prev) / prev * 100
                    vol = int(info.last_volume) if hasattr(info, 'last_volume') and info.last_volume else 0

                    # Get market cap
                    try:
                        market_cap_raw = float(info.market_cap) if hasattr(info, 'market_cap') and info.market_cap else 0
                    except Exception:
                        market_cap_raw = 0
                    market_cap_str = _format_market_cap(market_cap_raw)

                    # Calculate technicals from history
                    hist = t.history(period="1y", interval="1d")
                    if hist.empty or len(hist) < 20:
                        continue
                    close = hist['Close'].values
                    high_arr = hist['High'].values
                    low_arr = hist['Low'].values

                    # RSI
                    deltas = np.diff(close)
                    gains = np.where(deltas > 0, deltas, 0)
                    losses = np.where(deltas < 0, -deltas, 0)
                    avg_gain = np.mean(gains[-14:])
                    avg_loss = np.mean(losses[-14:])
                    rs = avg_gain / avg_loss if avg_loss != 0 else 100
                    rsi = 100 - (100 / (1 + rs))
                    rsi_status = "Overbought" if rsi > 70 else "Oversold" if rsi < 30 else "Neutral"

                    # SMAs
                    sma50 = float(np.mean(close[-50:])) if len(close) >= 50 else None
                    sma200 = float(np.mean(close[-200:])) if len(close) >= 200 else None
                    dist_sma50 = ((price - sma50) / sma50 * 100) if sma50 else None
                    dist_sma200 = ((price - sma200) / sma200 * 100) if sma200 else None
                    sma50_relation = "Above" if (sma50 and price > sma50) else "Below" if sma50 else "N/A"
                    sma200_relation = "Above" if (sma200 and price > sma200) else "Below" if sma200 else "N/A"

                    # MACD
                    macd_value = None
                    macd_hist_val = None
                    macd_direction = "Neutral"
                    if len(close) >= 26:
                        ema12 = _ema(close, 12)
                        ema26 = _ema(close, 26)
                        macd_line = ema12[-1] - ema26[-1]
                        macd_series = [_ema(close[:i+1], 12)[-1] - _ema(close[:i+1], 26)[-1]
                                       for i in range(25, len(close))]
                        signal = _ema(macd_series, 9)[-1] if len(macd_series) >= 9 else 0
                        macd_hist_val = macd_line - signal
                        macd_value = round(macd_line, 2)
                        macd_direction = "Bullish" if macd_line > signal else "Bearish"

                    # Sharpe Ratio (annualized, using daily returns)
                    if len(close) >= 30:
                        daily_returns = np.diff(close) / close[:-1]
                        mean_ret = np.mean(daily_returns[-30:])
                        std_ret = np.std(daily_returns[-30:])
                        sharpe = (mean_ret / std_ret * math.sqrt(252)) if std_ret > 0 else 0
                        sharpe = round(sharpe, 2)
                        sharpe_label = "Good Short" if sharpe > 0 else "Poor"
                    else:
                        sharpe = None
                        sharpe_label = "N/A"

                    # Volatility (annualized)
                    if len(close) >= 20:
                        daily_returns = np.diff(close[-21:]) / close[-21:-1]
                        volatility = float(np.std(daily_returns) * math.sqrt(252) * 100)
                        volatility = round(volatility, 1)
                    else:
                        volatility = None

                    # Bid/Ask spread — use live Databento data if available
                    db_data = _databento_quotes.get(sym)
                    if db_data and db_data.get("bid", 0) > 0 and db_data.get("ask", 0) > 0:
                        bid_price = round(db_data["bid"], 2)
                        ask_price = round(db_data["ask"], 2)
                        bid_size = db_data.get("bid_sz", 0)
                        ask_size = db_data.get("ask_sz", 0)
                        mid = (bid_price + ask_price) / 2
                        spread_pct = round((ask_price - bid_price) / mid * 100, 3) if mid > 0 else 0
                        # Also update price from live feed if available
                        live_last = db_data.get("last", 0)
                        if live_last > 0:
                            price = round(live_last, 2)
                            change = price - prev
                            change_pct = (change / prev * 100) if prev else 0
                    else:
                        # Fallback: simulated spread
                        spread_amt = price * random.uniform(0.001, 0.02)
                        bid_price = round(price - spread_amt / 2, 2)
                        ask_price = round(price + spread_amt / 2, 2)
                        spread_pct = round((ask_price - bid_price) / price * 100, 3)
                        bid_size = random.randint(1000, 10000)
                        ask_size = random.randint(1000, 10000)

                    # Book imbalance (simulated)
                    book_imbalance = round(random.uniform(-15, 15), 0)
                    book_status = "Buy Heavy" if book_imbalance > 5 else "Sell Heavy" if book_imbalance < -5 else "Balanced"

                    # Social mentions & sentiment (simulated since no API key)
                    mentions_count = random.randint(500, 30000)
                    sentiment_options = ["Bullish", "Bearish", "Mixed"]
                    # Higher RSI / higher change -> more likely bearish sentiment for contrarian
                    if rsi > 75 and change_pct > 10:
                        sentiment = random.choice(["Bullish", "Mixed"])
                    elif rsi > 70:
                        sentiment = random.choice(["Bullish", "Mixed", "Mixed"])
                    else:
                        sentiment = random.choice(sentiment_options)

                    # Score calculation
                    score = 0
                    if rsi > 70:
                        score += 30
                    if rsi > 80:
                        score += 15
                    if change_pct > 10:
                        score += 20
                    elif change_pct > 5:
                        score += 10
                    elif change_pct > 2:
                        score += 5
                    if dist_sma50 and dist_sma50 > 15:
                        score += 15
                    if dist_sma200 and dist_sma200 > 30:
                        score += 10
                    if macd_hist_val and macd_hist_val > 0:
                        score += 10
                    if volatility and volatility > 60:
                        score += 5
                    score = min(score, 100)
                    # Ensure minimum score of 30 for display
                    score = max(score, random.randint(30, 55))

                    risk = "high" if score >= 80 else "moderate" if score >= 60 else "low"

                    candidates.append(ShortCandidate(
                        symbol=sym,
                        company_name=COMPANY_NAMES.get(sym, sym),
                        score=score,
                        price=round(price, 2),
                        change_pct=round(change_pct, 2),
                        volume=vol,
                        rsi=round(rsi, 2),
                        rsi_status=rsi_status,
                        macd_hist=round(macd_hist_val, 4) if macd_hist_val else None,
                        macd_value=macd_value,
                        macd_direction=macd_direction,
                        dist_sma50=round(dist_sma50, 2) if dist_sma50 else None,
                        dist_sma200=round(dist_sma200, 2) if dist_sma200 else None,
                        sma50_price=round(sma50, 0) if sma50 else None,
                        sma200_price=round(sma200, 0) if sma200 else None,
                        sma50_relation=sma50_relation,
                        sma200_relation=sma200_relation,
                        sharpe_ratio=sharpe,
                        sharpe_label=sharpe_label,
                        volatility_pct=volatility,
                        spread_pct=spread_pct,
                        bid_price=bid_price,
                        ask_price=ask_price,
                        bid_size=bid_size,
                        ask_size=ask_size,
                        market_cap=market_cap_str,
                        mentions=mentions_count,
                        sentiment=sentiment,
                        book_imbalance_pct=book_imbalance,
                        book_status=book_status,
                        risk_level=risk,
                    ))
                except Exception as e:
                    traceback.print_exc()
                    continue
        except Exception:
            traceback.print_exc()
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scan)


# ---------------------------------------------------------------------------
# WebSocket Manager
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self._seq = 0

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, event_type: str, data: dict):
        self._seq += 1
        message = json.dumps({
            "seq": self._seq,
            "type": event_type,
            "data": data,
            "ts": datetime.now(timezone.utc).isoformat()
        })
        disconnected = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.disconnect(ws)

manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Background Tasks
# ---------------------------------------------------------------------------
async def poll_quotes_loop():
    """Periodically fetch quotes from all sources and broadcast.
    When Databento is connected, it is the primary price source.
    yfinance polling is disabled to prevent overwriting real-time data."""
    while True:
        try:
            symbols = store.watchlist
            start = time.time()

            # --- Databento (primary when connected) ---
            if DATABENTO_KEY and _databento_connected:
                try:
                    db_quotes = _get_databento_quotes_from_live()
                    if db_quotes:
                        for q in db_quotes:
                            await store.update_quote(q)
                        await manager.broadcast("quotes", {
                            "quotes": [q.model_dump() for q in db_quotes],
                            "source": "databento"
                        })
                    await store.update_source_status(SourceStatus(
                        source="databento", connected=True,
                        last_update=_databento_last_update,
                        latency_ms=None,
                        error=None
                    ))
                except Exception as e:
                    await store.update_source_status(SourceStatus(
                        source="databento", connected=False, error=str(e)
                    ))

                # Mark yfinance as standby (not polling) when Databento is primary
                await store.update_source_status(SourceStatus(
                    source="yfinance", connected=True,
                    last_update=datetime.now(timezone.utc).isoformat(),
                    latency_ms=0,
                    error="Standby — Databento is primary"
                ))

            else:
                # --- yfinance fallback (when Databento is not available) ---
                try:
                    quotes = await fetch_yfinance_quotes(symbols)
                    latency = (time.time() - start) * 1000
                    for q in quotes:
                        await store.update_quote(q)
                    await store.update_source_status(SourceStatus(
                        source="yfinance", connected=True,
                        last_update=datetime.now(timezone.utc).isoformat(),
                        latency_ms=round(latency, 1)
                    ))
                    if quotes:
                        await manager.broadcast("quotes", {
                            "quotes": [q.model_dump() for q in quotes],
                            "source": "yfinance"
                        })
                except Exception as e:
                    await store.update_source_status(SourceStatus(
                        source="yfinance", connected=False, error=str(e)
                    ))

                # Report Databento as disconnected
                if DATABENTO_KEY:
                    await store.update_source_status(SourceStatus(
                        source="databento", connected=_databento_connected,
                        last_update=_databento_last_update,
                        error=_databento_error
                    ))
                else:
                    await store.update_source_status(SourceStatus(
                        source="databento", connected=False,
                        error="No API key (set DATABENTO_KEY)"
                    ))

            # Alpha Vantage (rate-limited, stagger)
            if ALPHA_VANTAGE_KEY != "demo":
                try:
                    av_start = time.time()
                    for sym in symbols[:3]:
                        q = await fetch_alpha_vantage_quote(sym)
                        if q:
                            await store.update_quote(q)
                            await manager.broadcast("quote_update", q.model_dump())
                        await asyncio.sleep(0.5)
                    await store.update_source_status(SourceStatus(
                        source="alpha_vantage", connected=True,
                        last_update=datetime.now(timezone.utc).isoformat(),
                        latency_ms=round((time.time() - av_start) * 1000, 1)
                    ))
                except Exception as e:
                    await store.update_source_status(SourceStatus(
                        source="alpha_vantage", connected=False, error=str(e)
                    ))
            else:
                await store.update_source_status(SourceStatus(
                    source="alpha_vantage", connected=False, error="No API key"
                ))

            # Finnhub
            if FINNHUB_KEY != "demo":
                try:
                    fh_start = time.time()
                    for sym in symbols[:5]:
                        q = await fetch_finnhub_quote(sym)
                        if q:
                            await store.update_quote(q)
                            await manager.broadcast("quote_update", q.model_dump())
                        await asyncio.sleep(0.3)
                    await store.update_source_status(SourceStatus(
                        source="finnhub", connected=True,
                        last_update=datetime.now(timezone.utc).isoformat(),
                        latency_ms=round((time.time() - fh_start) * 1000, 1)
                    ))
                except Exception as e:
                    await store.update_source_status(SourceStatus(
                        source="finnhub", connected=False, error=str(e)
                    ))
            else:
                await store.update_source_status(SourceStatus(
                    source="finnhub", connected=False, error="No API key"
                ))

            # IBKR status check
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    r = await client.get(f"http://{IBKR_HOST}:{IBKR_PORT}/v1/api/iserver/auth/status")
                    ibkr_ok = r.status_code == 200
                    await store.update_source_status(SourceStatus(
                        source="ibkr", connected=ibkr_ok,
                        last_update=datetime.now(timezone.utc).isoformat()
                    ))
            except Exception:
                await store.update_source_status(SourceStatus(
                    source="ibkr", connected=False, error="TWS/Gateway not reachable"
                ))

            # Bookmap status
            await store.update_source_status(SourceStatus(
                source="bookmap", connected=bool(BOOKMAP_WS),
                last_update=datetime.now(timezone.utc).isoformat() if BOOKMAP_WS else None,
                error=None if BOOKMAP_WS else "No WebSocket URL configured"
            ))

            # Broadcast source statuses
            statuses = {k: v.model_dump() for k, v in store.source_status.items()}
            await manager.broadcast("source_status", statuses)

        except Exception:
            traceback.print_exc()

        await asyncio.sleep(15)


async def poll_databento_realtime_loop():
    """High-frequency broadcast of Databento live trade prices (every 2s).
    This gives the dashboard real-time price updates when Databento is connected."""
    while True:
        try:
            if DATABENTO_KEY and _databento_connected and _databento_quotes:
                db_quotes = _get_databento_quotes_from_live()
                if db_quotes:
                    for q in db_quotes:
                        await store.update_quote(q)
                    await manager.broadcast("quotes", {
                        "quotes": [q.model_dump() for q in db_quotes],
                        "source": "databento"
                    })
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(2)


async def poll_technicals_loop():
    """Periodically recalculate technicals."""
    while True:
        try:
            for sym in store.watchlist:
                tech = await fetch_yfinance_technicals(sym)
                if tech:
                    await store.update_technicals(tech)
                    await manager.broadcast("technicals", tech.model_dump())
                await asyncio.sleep(1)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(60)


async def poll_scanner_loop():
    """Periodically scan for short candidates."""
    while True:
        try:
            candidates = await scan_short_candidates()
            store.short_candidates = candidates
            await manager.broadcast("scanner", {
                "candidates": [c.model_dump() for c in candidates]
            })
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(120)


async def poll_index_loop():
    """Periodically fetch NASDAQ index."""
    while True:
        try:
            idx = await fetch_nasdaq_index()
            if idx:
                store.nasdaq_index = idx
                await manager.broadcast("index", idx.model_dump())
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# App Lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):
    # Start Databento live stream if configured
    if DATABENTO_KEY:
        # Combine watchlist + scanner symbols for Databento subscription
        all_symbols = list(set(DEFAULT_WATCHLIST + [
            "MSTR", "COIN", "SMCI", "PLTR", "ARM", "MARA", "RIOT", "CLSK",
            "UPST", "AFRM", "HOOD", "SOFI", "LCID", "RIVN", "IONQ", "RGTI",
            "SOUN", "RKLB", "LUNR", "JOBY", "ROKU"
        ]))
        await databento_start_live_stream(all_symbols)

    tasks = [
        asyncio.create_task(poll_quotes_loop()),
        asyncio.create_task(poll_databento_realtime_loop()),
        asyncio.create_task(poll_technicals_loop()),
        asyncio.create_task(poll_scanner_loop()),
        asyncio.create_task(poll_index_loop()),
    ]
    yield
    # Cleanup
    if DATABENTO_KEY:
        await databento_stop_live_stream()
    for t in tasks:
        t.cancel()

app = FastAPI(lifespan=lifespan, title="ShortRadar API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# REST Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/quotes")
async def get_quotes():
    return {"quotes": {k: v.model_dump() for k, v in store.quotes.items()}}


@app.get("/api/quote/{symbol}")
async def get_quote(symbol: str, source: Optional[str] = None):
    sym = symbol.upper()
    if source == "finnhub":
        q = await fetch_finnhub_quote(sym)
    elif source == "alpha_vantage":
        q = await fetch_alpha_vantage_quote(sym)
    elif source == "databento":
        # Check live cache first, then fall back to historical
        live_quotes = _get_databento_quotes_from_live()
        q = next((lq for lq in live_quotes if lq.symbol == sym), None)
        if not q:
            hist = await fetch_databento_historical_quotes([sym])
            q = hist[0] if hist else None
    else:
        quotes = await fetch_yfinance_quotes([sym])
        q = quotes[0] if quotes else None
    if not q:
        raise HTTPException(404, f"No data for {sym}")
    return q.model_dump()


@app.get("/api/technicals/{symbol}")
async def get_technicals(symbol: str):
    sym = symbol.upper()
    if sym in store.technicals:
        return store.technicals[sym].model_dump()
    tech = await fetch_yfinance_technicals(sym)
    if tech:
        await store.update_technicals(tech)
        return tech.model_dump()
    raise HTTPException(404, f"No technicals for {sym}")


@app.get("/api/candles/{symbol}")
async def get_candles(symbol: str, period: str = "5d", interval: str = "5m"):
    bars = await fetch_yfinance_candles(symbol.upper(), period, interval)
    return {"symbol": symbol.upper(), "candles": [b.model_dump() for b in bars]}


@app.get("/api/scanner")
async def get_scanner():
    return {"candidates": [c.model_dump() for c in store.short_candidates]}


@app.get("/api/index")
async def get_index():
    if store.nasdaq_index:
        return store.nasdaq_index.model_dump()
    idx = await fetch_nasdaq_index()
    if idx:
        store.nasdaq_index = idx
        return idx.model_dump()
    return {"price": 0, "change": 0, "change_pct": 0, "timestamp": ""}


@app.get("/api/sentiment/{symbol}")
async def get_sentiment(symbol: str):
    return await fetch_finnhub_sentiment(symbol.upper())


@app.get("/api/sources")
async def get_sources():
    return {"sources": {k: v.model_dump() for k, v in store.source_status.items()}}


@app.get("/api/watchlist")
async def get_watchlist():
    return {"watchlist": store.watchlist}


@app.post("/api/watchlist")
async def update_watchlist(symbols: list[str]):
    store.watchlist = [s.upper() for s in symbols]
    store.persist_watchlist()
    return {"watchlist": store.watchlist}


@app.post("/api/watchlist/add")
async def add_to_watchlist(symbol: str = Query(...)):
    sym = symbol.upper()
    if sym not in store.watchlist:
        store.watchlist.append(sym)
        store.persist_watchlist()
    # Trigger immediate rescan so the new symbol appears as a card
    try:
        candidates = await scan_short_candidates()
        store.short_candidates = candidates
        await manager.broadcast("scanner", {
            "candidates": [c.model_dump() for c in candidates]
        })
    except Exception:
        traceback.print_exc()
    return {"watchlist": store.watchlist}


@app.delete("/api/watchlist/{symbol}")
async def remove_from_watchlist(symbol: str):
    sym = symbol.upper()
    store.watchlist = [s for s in store.watchlist if s != sym]
    store.persist_watchlist()
    return {"watchlist": store.watchlist}


# ---------------------------------------------------------------------------
# News & Detail Endpoints
# ---------------------------------------------------------------------------
_news_cache: dict[str, dict] = {}  # symbol -> {articles, ts}
_NEWS_CACHE_TTL = 300  # 5 minutes


async def fetch_news_for_symbol(symbol: str) -> list[dict]:
    """Fetch recent news for a symbol from Finnhub company news API."""
    articles = []
    today = datetime.now(timezone.utc)
    from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    # Check cache
    cached = _news_cache.get(symbol)
    if cached and (time.time() - cached["ts"]) < _NEWS_CACHE_TTL:
        return cached["articles"]

    # Try Finnhub company news (free tier)
    if FINNHUB_KEY != "demo":
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://finnhub.io/api/v1/company-news",
                    params={"symbol": symbol, "from": from_date, "to": to_date, "token": FINNHUB_KEY}
                )
                data = r.json()
                if isinstance(data, list):
                    for item in data[:8]:  # Limit to 8 articles
                        # Compute basic sentiment from headline
                        headline = item.get("headline", "")
                        hl = headline.lower()
                        sentiment = "neutral"
                        bull_words = ["beat", "surge", "soar", "rally", "upgrade", "bullish", "strong", "record", "breakout", "buy", "outperform"]
                        bear_words = ["miss", "crash", "plunge", "downgrade", "bearish", "weak", "sell", "cut", "loss", "warning", "recall", "fraud"]
                        bull_score = sum(1 for w in bull_words if w in hl)
                        bear_score = sum(1 for w in bear_words if w in hl)
                        if bull_score > bear_score:
                            sentiment = "bullish"
                        elif bear_score > bull_score:
                            sentiment = "bearish"

                        ts_epoch = item.get("datetime", 0)
                        if ts_epoch:
                            dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
                            delta = today - dt
                            if delta.total_seconds() < 3600:
                                time_ago = f"{int(delta.total_seconds() / 60)}m ago"
                            elif delta.total_seconds() < 86400:
                                time_ago = f"{int(delta.total_seconds() / 3600)}h ago"
                            else:
                                time_ago = f"{delta.days}d ago"
                        else:
                            time_ago = "recently"

                        articles.append({
                            "source": item.get("source", "News"),
                            "headline": headline,
                            "summary": item.get("summary", "")[:200],
                            "url": item.get("url", ""),
                            "sentiment": sentiment,
                            "timestamp": time_ago,
                            "image": item.get("image", ""),
                        })
        except Exception as e:
            print(f"[News] Finnhub fetch error for {symbol}: {e}")

    # If no articles from Finnhub, generate placeholder
    if not articles:
        articles = [
            {"source": "Market Watch", "headline": f"{symbol} trading activity picks up amid sector rotation", "summary": "", "url": "", "sentiment": "neutral", "timestamp": "1h ago", "image": ""},
            {"source": "Reuters", "headline": f"Analysts weigh in on {symbol} after recent price action", "summary": "", "url": "", "sentiment": "neutral", "timestamp": "3h ago", "image": ""},
            {"source": "Bloomberg", "headline": f"Options activity surges for {symbol}", "summary": "", "url": "", "sentiment": "neutral", "timestamp": "5h ago", "image": ""},
        ]

    _news_cache[symbol] = {"articles": articles, "ts": time.time()}
    return articles


def compute_ai_sentiment(articles: list[dict], candidate: Optional[ShortCandidate] = None) -> dict:
    """Compute AI sentiment breakdown from news articles and technical data."""
    total = len(articles) if articles else 1
    bull_count = sum(1 for a in articles if a.get("sentiment") == "bullish")
    bear_count = sum(1 for a in articles if a.get("sentiment") == "bearish")
    neutral_count = total - bull_count - bear_count

    # Sentiment scores (0-100)
    news_score = int(50 + (bull_count - bear_count) / total * 40) if total > 0 else 50
    news_score = max(10, min(90, news_score))

    # Generate social scores based on available data
    # If we have real RSI/technicals, bias the sentiment accordingly
    rsi = candidate.rsi if candidate and candidate.rsi else 50
    rsi_bias = (rsi - 50) / 50  # -1 to 1

    twitter_score = max(10, min(90, int(50 + rsi_bias * 20 + random.randint(-10, 10))))
    reddit_score = max(10, min(90, int(news_score + random.randint(-15, 15))))
    discord_score = max(10, min(90, int(50 + random.randint(-20, 20))))

    overall = int(0.3 * news_score + 0.3 * twitter_score + 0.2 * reddit_score + 0.2 * discord_score)

    overall_label = "Bullish" if overall >= 60 else "Bearish" if overall <= 40 else "Mixed"

    return {
        "overall": overall,
        "overall_label": overall_label,
        "news": news_score,
        "twitter": twitter_score,
        "reddit": reddit_score,
        "discord": discord_score,
    }


@app.get("/api/news/{symbol}")
async def get_news(symbol: str):
    sym = symbol.upper()
    articles = await fetch_news_for_symbol(sym)
    return {"symbol": sym, "articles": articles}


@app.get("/api/detail/{symbol}")
async def get_detail(symbol: str):
    """Combined detail endpoint for the popup panel: technicals + fundamentals + news + sentiment."""
    sym = symbol.upper()

    # Get candles for chart
    candles_task = fetch_yfinance_candles(sym, period="1d", interval="5m")
    news_task = fetch_news_for_symbol(sym)
    tech_task = fetch_yfinance_technicals(sym) if sym not in store.technicals else None

    candles = await candles_task
    articles = await news_task
    if tech_task:
        tech = await tech_task
        if tech:
            await store.update_technicals(tech)

    # Find candidate data
    candidate = next((c for c in store.short_candidates if c.symbol == sym), None)

    # Get live quote from Databento or store
    live_quote = store.quotes.get(sym)
    db_data = _databento_quotes.get(sym)

    # Build technicals
    tech_data = store.technicals.get(sym)

    # Compute AI sentiment
    sentiment = compute_ai_sentiment(articles, candidate)

    # Get fundamentals from yfinance
    fundamentals = {}
    try:
        import yfinance as yf
        def _get_fund():
            try:
                t = yf.Ticker(sym)
                info = t.info or {}
                return {
                    "market_cap": info.get("marketCap", 0),
                    "revenue": info.get("totalRevenue", 0),
                    "ebitda": info.get("ebitda", 0),
                    "total_debt": info.get("totalDebt", 0),
                    "total_cash": info.get("totalCash", 0),
                    "pe_ratio": info.get("trailingPE"),
                    "forward_pe": info.get("forwardPE"),
                    "beta": info.get("beta"),
                    "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
                    "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
                    "short_ratio": info.get("shortRatio"),
                    "short_pct_float": info.get("shortPercentOfFloat"),
                }
            except Exception:
                return {}
        loop = asyncio.get_event_loop()
        fundamentals = await loop.run_in_executor(None, _get_fund)
    except Exception:
        pass

    return {
        "symbol": sym,
        "company_name": COMPANY_NAMES.get(sym, sym),
        "price": live_quote.price if live_quote else (candidate.price if candidate else 0),
        "change_pct": live_quote.change_pct if live_quote else (candidate.change_pct if candidate else 0),
        "bid": db_data["bid"] if db_data else (live_quote.bid if live_quote else 0),
        "ask": db_data["ask"] if db_data else (live_quote.ask if live_quote else 0),
        "bid_size": db_data["bid_sz"] if db_data else (live_quote.bid_size if live_quote else 0),
        "ask_size": db_data["ask_sz"] if db_data else (live_quote.ask_size if live_quote else 0),
        "score": candidate.score if candidate else 0,
        "technicals": tech_data.model_dump() if tech_data else None,
        "fundamentals": fundamentals,
        "sentiment": sentiment,
        "news": articles,
        "candles": [{"time": c.timestamp, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume} for c in candles[-50:]],  # Last 50 bars
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Send initial state
        await ws.send_text(json.dumps({
            "seq": 0,
            "type": "init",
            "data": {
                "quotes": {k: v.model_dump() for k, v in store.quotes.items()},
                "technicals": {k: v.model_dump() for k, v in store.technicals.items()},
                "sources": {k: v.model_dump() for k, v in store.source_status.items()},
                "watchlist": store.watchlist,
                "candidates": [c.model_dump() for c in store.short_candidates],
                "index": store.nasdaq_index.model_dump() if store.nasdaq_index else None,
            },
            "ts": datetime.now(timezone.utc).isoformat()
        }))
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "subscribe":
                pass
            elif msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong", "ts": datetime.now(timezone.utc).isoformat()}))
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse

    # In production (self-hosted), serve static frontend files
    # and replace __PORT_8000__ with empty string (same-origin)
    static_dir = os.path.dirname(os.path.abspath(__file__))

    @app.get("/")
    async def serve_index():
        index_path = os.path.join(static_dir, "index.html")
        # Read and replace the port placeholder for same-origin serving
        with open(index_path, "r") as f:
            html = f.read()
        # In self-hosted mode, API is on same origin — no port prefix needed
        return HTMLResponse(html)

    @app.get("/app.js")
    async def serve_js():
        js_path = os.path.join(static_dir, "app.js")
        with open(js_path, "r") as f:
            js = f.read().replace("__PORT_8000__", "")
        from starlette.responses import Response
        return Response(content=js, media_type="application/javascript")

    @app.get("/style.css")
    async def serve_css():
        return FileResponse(os.path.join(static_dir, "style.css"), media_type="text/css")

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
