# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ShortRadar Nexus is a real-time short opportunity scanner — a single-page trading dashboard backed by a Python FastAPI server. The frontend is vanilla JS/CSS (no build step, no framework). The backend aggregates data from multiple financial APIs and streams updates via WebSocket.

## Commands

```bash
# Run locally (serves both API and frontend on :8000)
python -u api_server.py

# Required env var
export DATABENTO_KEY="db-..."
# Optional: FINNHUB_KEY, ALPHA_VANTAGE_KEY

# Docker
docker build -t shortradar-nexus .
docker run -p 8000:8000 -e DATABENTO_KEY="..." shortradar-nexus

# Lint frontend JS
npx eslint app.js
```

No test suite exists. No package.json — ESLint is run via npx with `eslint.config.mjs`.

## Architecture

**Single-file backend** (`api_server.py`, ~1700 lines): FastAPI app serving REST endpoints, WebSocket, and static files (index.html, app.js, style.css, assets/).

### Backend Key Components

- **Data Models** (lines ~60-155): Pydantic models — `Quote`, `TechnicalIndicators`, `ShortCandidate`, `CandleBar`, `NASDAQIndex`
- **DataStore** (line ~188): In-memory singleton (`store`) holding quotes, technicals, candles, candidates, watchlist. Watchlist persists to `data/watchlist.json`.
- **Data Source Adapters** (lines ~220-800): Async functions fetching from Alpha Vantage (quotes, daily, intraday, technicals), Finnhub (quotes, sentiment, news), and Databento (live mbp-1 stream + historical). Each adapter is independent.
- **Databento Live Stream** (line ~560): Runs in a background thread via `databento.Live`, feeds bid/ask into a global dict (`_databento_live_data`). Started/stopped via `databento_start_live_stream()` / `databento_stop_live_stream()`.
- **Scanner** (`scan_short_candidates`, line ~827): Core scoring logic — computes RSI, MACD, SMA distances, Sharpe ratio, spread, book imbalance, and produces a sorted `ShortCandidate` list.
- **Background Loops** (lines ~1126-1316): `poll_quotes_loop` (2s), `poll_databento_realtime_loop` (2s), `poll_technicals_loop` (300s), `poll_scanner_loop` (120s), `poll_index_loop` (15s). All launched in `lifespan()`.
- **ConnectionManager** (line ~1090): WebSocket connection manager for broadcasting updates to all connected clients.
- **Detail/News endpoints** (lines ~1475-1680): `/api/detail/{symbol}` aggregates quotes, technicals, candles, news, AI sentiment into a single response. `/api/news/{symbol}` fetches from Finnhub.

### Frontend

- **index.html**: Static shell with CDN-loaded Chart.js
- **app.js**: Vanilla JS SPA — connects WebSocket, renders card grid, detail panel with charts, ticker scroll. State is a single `state` object.
- **style.css**: Dark theme dashboard styling

### Deployment

Configured for Railway (`railway.json`, `railway.toml`) with Dockerfile. Health check at `/api/health`. Also deployable to Render, Fly.io, or any Docker host.

## Key Patterns

- All external API calls use `httpx.AsyncClient` with timeouts and try/except — failures are logged and skipped, never crash the server.
- Technicals (RSI, MACD, Bollinger, ATR, VWAP, Sharpe) are computed from raw daily price data fetched via Alpha Vantage, not from a TA library.
- The frontend uses no API prefix by default (`API = "__PORT_8000__"` is a placeholder); all fetch calls go to relative paths like `/api/scanner`.
- WebSocket messages are JSON with a `type` field: `"candidates"`, `"quote_update"`, `"index"`, `"sources"`.
