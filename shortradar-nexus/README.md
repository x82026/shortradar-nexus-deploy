# ShortRadar Nexus — Real-time Short Opportunity Scanner

Multi-source trading dashboard with real-time Databento bid/ask streaming, technical analysis, AI sentiment, and news feeds.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Browser (index.html + app.js + style.css)      │
│  ├─ WebSocket ──► /ws (real-time updates)       │
│  ├─ REST ──► /api/scanner, /api/detail/{sym}    │
│  └─ Click card ──► Detail Panel (news/sentiment)│
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│  FastAPI Server (api_server.py)  :8000           │
│  ├─ Databento mbp-1 live stream (best bid/offer)│
│  ├─ yfinance (technicals, fundamentals, candles)│
│  ├─ Finnhub (news, social sentiment)            │
│  ├─ Alpha Vantage (quotes backup)               │
│  └─ Scanner loop (120s) + Quote poll (2s)       │
└─────────────────────────────────────────────────┘
```

## Quick Start (Local)

```bash
# Clone / unzip
cd shortradar-nexus

# Install dependencies
pip install -r requirements.txt

# Set your API keys
export DATABENTO_KEY="db-your-key-here"
# Optional:
# export FINNHUB_KEY="your-finnhub-key"
# export ALPHA_VANTAGE_KEY="your-av-key"

# Run
python -u api_server.py
```

Open **http://localhost:8000** — the server serves the frontend directly.

## Deploy to Railway (Recommended)

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "ShortRadar Nexus"
git remote add origin https://github.com/YOUR_USER/shortradar-nexus.git
git push -u origin main
```

### 2. Connect to Railway

1. Go to [railway.com](https://railway.com) and sign in with GitHub
2. Click **New Project → Deploy from GitHub Repo**
3. Select your `shortradar-nexus` repo
4. Railway auto-detects the `Dockerfile` and `railway.json`

### 3. Set Environment Variables

In the Railway dashboard, go to **Variables** and add:

| Variable | Value | Required |
|----------|-------|----------|
| `DATABENTO_KEY` | `db-your-key-here` | Yes |
| `FINNHUB_KEY` | Your Finnhub API key | Optional |
| `ALPHA_VANTAGE_KEY` | Your Alpha Vantage key | Optional |
| `PORT` | `8000` | Auto-set by Railway |

### 4. Generate Domain

In Railway **Settings → Networking**, click **Generate Domain** to get a public URL like `shortradar-nexus-production.up.railway.app`.

That's it — your dashboard is live with real-time Databento streaming.

## Deploy with Docker (Any Platform)

```bash
# Build
docker build -t shortradar-nexus .

# Run
docker run -d \
  --name shortradar \
  -p 8000:8000 \
  -e DATABENTO_KEY="db-your-key-here" \
  -v shortradar-data:/app/data \
  shortradar-nexus
```

The `-v shortradar-data:/app/data` mounts a persistent volume so your watchlist survives container restarts.

## Deploy to Other Platforms

### Render

1. Push to GitHub
2. New Web Service → connect repo
3. **Environment**: Docker
4. Add `DATABENTO_KEY` env var
5. Deploy

### Fly.io

```bash
fly launch --image shortradar-nexus
fly secrets set DATABENTO_KEY="db-your-key-here"
fly deploy
```

### AWS Lightsail / EC2

```bash
# On your instance:
sudo apt update && sudo apt install -y python3-pip
git clone https://github.com/YOUR_USER/shortradar-nexus.git
cd shortradar-nexus
pip install -r requirements.txt
export DATABENTO_KEY="db-your-key-here"

# Run with systemd or screen
nohup python -u api_server.py > server.log 2>&1 &
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABENTO_KEY` | Databento API key for real-time mbp-1 feed | (none — required for live data) |
| `FINNHUB_KEY` | Finnhub API key for news & sentiment | `demo` |
| `ALPHA_VANTAGE_KEY` | Alpha Vantage API key for backup quotes | `demo` |
| `DATABENTO_LIVE_DATASET` | Databento dataset for live streaming | `EQUS.MINI` |
| `PORT` | Server port | `8000` |

## Data Persistence

- **Watchlist**: Saved to `data/watchlist.json` automatically when you add/remove symbols
- **Quotes/Technicals/Candidates**: In-memory only (rebuilt on startup from live data within ~30 seconds)

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/scanner` | All short candidates with scores |
| GET | `/api/detail/{symbol}` | Full detail (technicals, fundamentals, news, sentiment, chart) |
| GET | `/api/news/{symbol}` | News feed for a symbol |
| GET | `/api/watchlist` | Current watchlist |
| POST | `/api/watchlist/add?symbol=XYZ` | Add symbol to scanner |
| DELETE | `/api/watchlist/{symbol}` | Remove symbol |
| WS | `/ws` | WebSocket for real-time updates |

## Tech Stack

- **Backend**: Python 3.11, FastAPI, uvicorn
- **Real-time Data**: Databento (mbp-1 schema via EQUS.MINI)
- **Technicals**: yfinance + numpy
- **News**: Finnhub company news API
- **Frontend**: Vanilla JS, CSS (no framework — fast & lightweight)
