// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const API = "__PORT_8000__";
const WS_BASE = `${location.origin}${location.pathname.replace(/\/[^/]*$/, "").replace(/\/$/, "")}`;
const WS_PROTO = location.protocol === "https:" ? "wss:" : "ws:";
const WS_PATH = API
  ? `${WS_PROTO}//${location.host}${location.pathname.replace(/\/[^/]*$/, "").replace(/\/$/, "")}/${API}/ws`
  : `${WS_PROTO}//${location.host}/ws`;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let state = {
  candidates: [],
  index: null,
  sources: {},
  wsConnected: false,
  lastUpdate: null,
};

let ws = null;
let pollTimer = null;

// ---------------------------------------------------------------------------
// DOM Refs
// ---------------------------------------------------------------------------
const $tickerScroll = document.getElementById("ticker-scroll");
const $cardGrid = document.getElementById("card-grid");
const $nasdaqPrice = document.getElementById("nasdaq-price");
const $nasdaqChange = document.getElementById("nasdaq-change");
const $topGainersCount = document.getElementById("top-gainers-count");
const $avgChange = document.getElementById("avg-change");
const $liveBadge = document.getElementById("live-badge");
const $updatedText = document.getElementById("updated-text");
const $refreshBtn = document.getElementById("refresh-btn");

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function formatPrice(n) {
  if (n === null || n === undefined) {return "—";}
  return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatPct(n) {
  if (n === null || n === undefined) {return "—";}
  const sign = n >= 0 ? "+" : "";
  return sign + Number(n).toFixed(2) + "%";
}

function formatVolume(v) {
  if (!v) {return "0";}
  if (v >= 1e9) {return (v / 1e9).toFixed(1) + "B";}
  if (v >= 1e6) {return (v / 1e6).toFixed(1) + "M";}
  if (v >= 1e3) {return (v / 1e3).toFixed(1) + "K";}
  return String(v);
}

function formatNum(n) {
  if (n === null || n === undefined) {return "—";}
  return Number(n).toLocaleString("en-US");
}

function timeStr(d) {
  if (!d) {d = new Date();}
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function riskClass(score) {
  if (score >= 80) {return "high";}
  if (score >= 60) {return "moderate";}
  return "low";
}

function colorForChange(val) {
  if (val > 0) {return "text-green";}
  if (val < 0) {return "text-red";}
  return "text-muted";
}

function trendIcon(val) {
  return val >= 0 ? "↗" : "↘";
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

// ---------------------------------------------------------------------------
// Ticker Tape
// ---------------------------------------------------------------------------
function renderTickerTape(candidates) {
  if (!candidates || candidates.length === 0) {return;}
  const items = candidates.map((c) => {
    const cls = c.change_pct >= 0 ? "text-green" : "text-red";
    return `<span class="ticker-item">
      <span class="symbol">${escapeHtml(c.symbol)}</span>
      <span class="price">${formatPrice(c.price)}</span>
      <span class="trend-icon ${cls}">${trendIcon(c.change_pct)}</span>
      <span class="change ${cls}">${formatPct(c.change_pct)}</span>
    </span>`;
  }).join("");
  // Duplicate for seamless loop
  $tickerScroll.innerHTML = items + items;
}

// ---------------------------------------------------------------------------
// Header Stats
// ---------------------------------------------------------------------------
function renderHeaderStats() {
  // NASDAQ Index
  if (state.index) {
    $nasdaqPrice.textContent = Number(state.index.price).toLocaleString("en-US", { minimumFractionDigits: 2 });
    const cls = state.index.change_pct >= 0 ? "text-green" : "text-red";
    $nasdaqChange.className = "change " + cls;
    $nasdaqChange.textContent = formatPct(state.index.change_pct);
  }

  // Top gainers & avg change
  const gainers = state.candidates.filter((c) => c.change_pct > 0);
  $topGainersCount.innerHTML = `<span class="trend-icon text-green">↗</span> ${gainers.length}`;
  if (gainers.length > 0) {
    const avg = gainers.reduce((sum, c) => sum + c.change_pct, 0) / gainers.length;
    $avgChange.className = "value";
    $avgChange.innerHTML = `<span class="change text-green">${formatPct(avg)}</span>`;
  }

  // Live badge
  if (state.wsConnected) {
    $liveBadge.className = "live-badge";
    $liveBadge.innerHTML = '<span class="pulse"></span> LIVE';
  } else {
    $liveBadge.className = "live-badge disconnected";
    $liveBadge.innerHTML = '<span class="pulse"></span> OFFLINE';
  }

  // Updated time
  $updatedText.textContent = "Updated: " + timeStr(state.lastUpdate);
}

// ---------------------------------------------------------------------------
// Source Pills
// ---------------------------------------------------------------------------
function renderSourcePills() {
  const pills = document.querySelectorAll(".source-pill");
  const sourceMap = {
    yfinance: "YF",
    alpha_vantage: "AV",
    finnhub: "FH",
    bookmap: "BM",
    ibkr: "IBKR",
    databento: "DB",
  };
  pills.forEach((pill) => {
    const key = pill.dataset.source;
    const src = state.sources[key];
    if (src && src.connected) {
      pill.classList.add("connected");
    } else {
      pill.classList.remove("connected");
    }
  });
}

// ---------------------------------------------------------------------------
// Candidate Cards
// ---------------------------------------------------------------------------
function renderCards(candidates) {
  if (!candidates || candidates.length === 0) {
    renderSkeletonCards();
    return;
  }

  const html = candidates.map((c) => {
    const risk = riskClass(c.score);
    const chgCls = colorForChange(c.change_pct);
    const abbrev = c.symbol.substring(0, 2);
    const hot = c.score >= 70;

    // MACD badge
    const macdCls = c.macd_direction === "Bullish" ? "bullish" : "bearish";
    const macdVal = c.macd_value !== null ? c.macd_value : "—";

    // RSI badge
    let rsiCls = "neutral";
    if (c.rsi_status === "Overbought") {rsiCls = "overbought";}
    else if (c.rsi_status === "Oversold") {rsiCls = "oversold";}
    const rsiVal = c.rsi !== null ? Math.round(c.rsi) : "—";

    // SMA relations
    const sma50RelCls = c.sma50_relation === "Above" ? "above" : c.sma50_relation === "Below" ? "below" : "";
    const sma200RelCls = c.sma200_relation === "Above" ? "above" : c.sma200_relation === "Below" ? "below" : "";
    const sharpeCls = c.sharpe_label === "Good Short" ? "good-short" : "poor";

    // Sentiment
    const sentCls = c.sentiment.toLowerCase();

    return `<div class="candidate-card" data-symbol="${escapeHtml(c.symbol)}" style="cursor:pointer">
      <div class="card-header">
        <div class="ticker-avatar">${escapeHtml(abbrev)}</div>
        <div class="card-header-text">
          <div class="ticker-row">
            <span class="ticker-sym">${escapeHtml(c.symbol)}</span>
            ${hot ? '<span class="fire-icon">🔥</span>' : ""}
          </div>
          <div class="company-name">${escapeHtml(c.company_name || c.symbol)}</div>
        </div>
        <div class="score-badge ${risk}">
          <span class="arrow">▲</span>
          <span class="score-num">${c.score}</span>
          <span class="score-label">SHORT</span>
        </div>
      </div>

      <div class="score-bar-container">
        <div class="score-bar-fill ${risk}" style="width: ${c.score}%"></div>
      </div>

      <div class="price-section">
        <span class="price">${formatPrice(c.price)}</span>
        <span class="change-row">
          <span class="trend-icon ${chgCls}">${trendIcon(c.change_pct)}</span>
          <span class="change-val ${chgCls}">${formatPct(c.change_pct)}</span>
        </span>
      </div>

      <div class="tech-badges">
        <span class="tech-badge ${macdCls}">
          <span class="badge-label">MACD:</span>
          <span class="badge-value">${macdVal}</span>
          <span class="badge-status">${escapeHtml(c.macd_direction)}</span>
        </span>
        <span class="tech-badge ${rsiCls}">
          <span class="badge-label">RSI:</span>
          <span class="badge-value">${rsiVal}</span>
          <span class="badge-status">${escapeHtml(c.rsi_status)}</span>
        </span>
      </div>

      <div class="sma-row">
        <span class="sma-item">
          <span class="sma-label">SMA50:</span>
          <span class="sma-value">${c.sma50_price !== null ? "$" + Math.round(c.sma50_price) : "—"}</span>
          <span class="sma-relation ${sma50RelCls}">${escapeHtml(c.sma50_relation)}</span>
        </span>
        <span class="sma-item">
          <span class="sma-label">SMA200:</span>
          <span class="sma-value">${c.sma200_price !== null ? "$" + Math.round(c.sma200_price) : "—"}</span>
          <span class="sma-relation ${sma200RelCls}">${escapeHtml(c.sma200_relation)}</span>
        </span>
        <span class="sma-item">
          <span class="sma-label">Sharpe:</span>
          <span class="sma-value">${c.sharpe_ratio !== null ? c.sharpe_ratio : "—"}</span>
          <span class="sma-relation ${sharpeCls}">${escapeHtml(c.sharpe_label)}</span>
        </span>
      </div>

      <div class="book-row">
        <span class="book-label">Book:</span>
        <span class="book-value">${Math.round(c.book_imbalance_pct)}%</span>
        <span class="book-status">${escapeHtml(c.book_status)}</span>
      </div>

      <div class="l2-section">
        <div class="l2-box bid">
          <div class="l2-label">Best Bid (L2)</div>
          <div class="l2-price">${c.bid_price !== null ? formatPrice(c.bid_price) : "—"}</div>
          <div class="l2-size">${formatNum(c.bid_size)} shares</div>
        </div>
        <div class="l2-box ask">
          <div class="l2-label">Best Ask (L2)</div>
          <div class="l2-price">${c.ask_price !== null ? formatPrice(c.ask_price) : "—"}</div>
          <div class="l2-size">${formatNum(c.ask_size)} shares</div>
        </div>
      </div>

      <div class="metrics-row">
        <div class="metric-item">
          <span class="metric-label">Volatility</span>
          <span class="metric-value">${c.volatility_pct !== null ? c.volatility_pct + "%" : "—"}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Spread</span>
          <span class="metric-value">${c.spread_pct !== null ? c.spread_pct + "%" : "—"}</span>
        </div>
      </div>

      <div class="metrics-row">
        <div class="metric-item">
          <span class="metric-label">Mkt Cap</span>
          <span class="metric-value">${escapeHtml(c.market_cap || "—")}</span>
        </div>
        <div class="metric-item">
          <span class="metric-label">Volume</span>
          <span class="metric-value">${formatVolume(c.volume)}</span>
        </div>
      </div>

      <div class="card-footer">
        <span class="mentions">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
          ${formatNum(c.mentions)} mentions
        </span>
        <span class="sentiment-badge ${sentCls}">
          <span class="sentiment-dot"></span>
          ${escapeHtml(c.sentiment)}
        </span>
      </div>
    </div>`;
  }).join("");

  $cardGrid.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Skeleton Cards
// ---------------------------------------------------------------------------
function renderSkeletonCards() {
  const skeletons = Array.from({ length: 8 }, () => `
    <div class="skeleton-card">
      <div class="skeleton-header">
        <div class="skeleton skeleton-circle"></div>
        <div style="flex:1">
          <div class="skeleton skeleton-line w-40"></div>
          <div class="skeleton skeleton-line w-60" style="height:10px"></div>
        </div>
        <div class="skeleton" style="width:56px;height:56px;border-radius:8px"></div>
      </div>
      <div class="skeleton skeleton-line w-full h-4" style="margin-bottom:14px"></div>
      <div class="skeleton skeleton-line w-60 h-8" style="margin-bottom:10px"></div>
      <div class="skeleton-badges">
        <div class="skeleton skeleton-badge"></div>
        <div class="skeleton skeleton-badge"></div>
      </div>
      <div class="skeleton-badges">
        <div class="skeleton skeleton-badge"></div>
        <div class="skeleton skeleton-badge"></div>
        <div class="skeleton skeleton-badge"></div>
      </div>
      <div class="skeleton skeleton-line w-40" style="margin-bottom:8px"></div>
      <div class="skeleton-grid">
        <div class="skeleton skeleton-box"></div>
        <div class="skeleton skeleton-box"></div>
      </div>
      <div class="skeleton-grid">
        <div class="skeleton skeleton-line w-80"></div>
        <div class="skeleton skeleton-line w-80"></div>
      </div>
      <div class="skeleton-grid">
        <div class="skeleton skeleton-line w-60"></div>
        <div class="skeleton skeleton-line w-60"></div>
      </div>
    </div>
  `).join("");
  $cardGrid.innerHTML = skeletons;
}

// ---------------------------------------------------------------------------
// Full render
// ---------------------------------------------------------------------------
function render() {
  renderHeaderStats();
  renderSourcePills();
  renderTickerTape(state.candidates);
  renderCards(state.candidates);
}

// ---------------------------------------------------------------------------
// Data Fetching (REST fallback)
// ---------------------------------------------------------------------------
async function fetchData() {
  try {
    const [scannerRes, indexRes, sourcesRes] = await Promise.all([
      fetch(`${API}/api/scanner`).then((r) => r.json()),
      fetch(`${API}/api/index`).then((r) => r.json()),
      fetch(`${API}/api/sources`).then((r) => r.json()),
    ]);

    if (scannerRes.candidates) {
      state.candidates = scannerRes.candidates.sort((a, b) => b.score - a.score);
    }
    if (indexRes && indexRes.price) {
      state.index = indexRes;
    }
    if (sourcesRes.sources) {
      state.sources = sourcesRes.sources;
    }
    state.lastUpdate = new Date();
    render();
  } catch (err) {
    console.warn("REST fetch failed:", err);
  }
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
function connectWebSocket() {
  try {
    ws = new WebSocket(WS_PATH);

    ws.onopen = function () {
      state.wsConnected = true;
      state.lastUpdate = new Date();
      renderHeaderStats();
      // Clear REST polling when WS is active
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    ws.onmessage = function (evt) {
      try {
        const msg = JSON.parse(evt.data);
        state.lastUpdate = new Date();

        if (msg.type === "init") {
          if (msg.data.candidates && msg.data.candidates.length > 0) {
            state.candidates = msg.data.candidates.sort((a, b) => b.score - a.score);
          }
          if (msg.data.index) {
            state.index = msg.data.index;
          }
          if (msg.data.sources) {
            state.sources = msg.data.sources;
          }
          render();
        } else if (msg.type === "scanner") {
          if (msg.data.candidates && msg.data.candidates.length > 0) {
            state.candidates = msg.data.candidates.sort((a, b) => b.score - a.score);
            renderTickerTape(state.candidates);
            renderCards(state.candidates);
            renderHeaderStats();
          }
        } else if (msg.type === "quotes") {
          // Update candidate prices in real-time from live feed
          if (msg.data.quotes && state.candidates.length > 0) {
            const quoteMap = {};
            for (const q of msg.data.quotes) {
              quoteMap[q.symbol] = q;
            }
            let changed = false;
            for (const c of state.candidates) {
              const q = quoteMap[c.symbol];
              if (q && q.price > 0) {
                c.price = q.price;
                if (q.change_pct !== 0) { c.change_pct = q.change_pct; }
                // Propagate real bid/ask from Databento mbp-1
                if (q.bid > 0) { c.bid_price = q.bid; }
                if (q.ask > 0) { c.ask_price = q.ask; }
                if (q.bid_size > 0) { c.bid_size = q.bid_size; }
                if (q.ask_size > 0) { c.ask_size = q.ask_size; }
                // Recalculate spread from real data
                if (q.bid > 0 && q.ask > 0 && q.ask > q.bid) {
                  const mid = (q.bid + q.ask) / 2;
                  c.spread_pct = mid > 0 ? Number(((q.ask - q.bid) / mid * 100).toFixed(3)) : 0;
                }
                changed = true;
              }
            }
            if (changed) {
              renderTickerTape(state.candidates);
              renderCards(state.candidates);
              renderHeaderStats();
            }
          }
        } else if (msg.type === "index") {
          state.index = msg.data;
          renderHeaderStats();
        } else if (msg.type === "source_status") {
          state.sources = msg.data;
          renderSourcePills();
        }
      } catch (e) {
        console.warn("WS parse error:", e);
      }
    };

    ws.onclose = function () {
      state.wsConnected = false;
      renderHeaderStats();
      // Fall back to REST polling
      if (!pollTimer) {
        pollTimer = setInterval(fetchData, 15000);
      }
      // Reconnect after 3s
      setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = function () {
      state.wsConnected = false;
      renderHeaderStats();
    };
  } catch (e) {
    console.warn("WS connect failed:", e);
    state.wsConnected = false;
    if (!pollTimer) {
      pollTimer = setInterval(fetchData, 15000);
    }
  }
}

// ---------------------------------------------------------------------------
// Refresh button
// ---------------------------------------------------------------------------
$refreshBtn.addEventListener("click", function () {
  $refreshBtn.classList.add("spinning");
  fetchData().finally(function () {
    setTimeout(function () {
      $refreshBtn.classList.remove("spinning");
    }, 800);
  });
});

// ---------------------------------------------------------------------------
// Add Symbol
// ---------------------------------------------------------------------------
const $addInput = document.getElementById("add-symbol-input");
const $addBtn = document.getElementById("add-symbol-btn");
const $addStatus = document.getElementById("add-symbol-status");
let statusTimer = null;

function showAddStatus(msg, type) {
  clearTimeout(statusTimer);
  $addStatus.textContent = msg;
  $addStatus.className = "add-symbol-status show " + type;
  statusTimer = setTimeout(function () {
    $addStatus.classList.remove("show");
  }, 3000);
}

async function addSymbol() {
  var raw = $addInput.value.trim().toUpperCase();
  if (!raw) { return; }
  // Basic validation: 1-6 uppercase letters
  if (!/^[A-Z]{1,6}$/.test(raw)) {
    showAddStatus("Invalid ticker format", "error");
    return;
  }
  // Check if already showing
  var existing = state.candidates.find(function (c) { return c.symbol === raw; });
  if (existing) {
    showAddStatus(raw + " already in scanner", "info");
    $addInput.value = "";
    return;
  }

  $addBtn.classList.add("loading");
  try {
    var res = await fetch(API + "/api/watchlist/add?symbol=" + encodeURIComponent(raw), { method: "POST" });
    if (!res.ok) { throw new Error("Server error"); }
    showAddStatus(raw + " added", "success");
    $addInput.value = "";
    // Refresh data to pick up the new symbol
    await fetchData();
  } catch (err) {
    showAddStatus("Failed to add " + raw, "error");
    console.warn("Add symbol error:", err);
  } finally {
    $addBtn.classList.remove("loading");
  }
}

$addBtn.addEventListener("click", addSymbol);
$addInput.addEventListener("keydown", function (e) {
  if (e.key === "Enter") { addSymbol(); }
});

// ---------------------------------------------------------------------------
// Detail Panel (Popup)
// ---------------------------------------------------------------------------
const $detailOverlay = document.getElementById("detail-overlay");
const $detailBackdrop = document.getElementById("detail-backdrop");
const $detailClose = document.getElementById("dp-close");
let detailSymbol = null;

function openDetailPanel(symbol) {
  detailSymbol = symbol;
  $detailOverlay.classList.remove("hidden");
  // Force reflow for animation
  void $detailOverlay.offsetHeight;
  $detailOverlay.classList.add("visible");
  document.body.style.overflow = "hidden";

  // Show loading state
  document.getElementById("dp-symbol").textContent = symbol;
  document.getElementById("dp-company").textContent = COMPANY_NAMES[symbol] || symbol;
  document.getElementById("dp-news-feed").innerHTML = '<div class="dp-loading">Loading data...</div>';
  document.getElementById("dp-fund-grid").innerHTML = '<div class="dp-loading">Loading...</div>';
  document.getElementById("dp-sentiment-bars").innerHTML = '<div class="dp-loading">Analyzing...</div>';

  // Fetch detail data
  fetch(`${API}/api/detail/${symbol}`)
    .then(function (r) { return r.json(); })
    .then(function (data) { renderDetailPanel(data); })
    .catch(function (err) {
      console.warn("Detail fetch error:", err);
      document.getElementById("dp-news-feed").innerHTML = '<div class="dp-loading">Error loading data</div>';
    });
}

function closeDetailPanel() {
  $detailOverlay.classList.remove("visible");
  document.body.style.overflow = "";
  detailSymbol = null;
  setTimeout(function () {
    $detailOverlay.classList.add("hidden");
  }, 350);
}

// Company names map for the panel
const COMPANY_NAMES = {
  MSTR: "MicroStrategy Inc", COIN: "Coinbase Global", SMCI: "Super Micro Computer",
  PLTR: "Palantir Technologies", ARM: "Arm Holdings", MARA: "Marathon Digital",
  RIOT: "Riot Platforms", CLSK: "CleanSpark Inc", UPST: "Upstart Holdings",
  AFRM: "Affirm Holdings", HOOD: "Robinhood Markets", SOFI: "SoFi Technologies",
  LCID: "Lucid Group", RIVN: "Rivian Automotive", IONQ: "IonQ Inc",
  RGTI: "Rigetti Computing", SOUN: "SoundHound AI", RKLB: "Rocket Lab USA",
  LUNR: "Intuitive Machines", JOBY: "Joby Aviation", ROKU: "Roku Inc",
  AAPL: "Apple Inc", MSFT: "Microsoft Corp", NVDA: "NVIDIA Corp",
  TSLA: "Tesla Inc", AMZN: "Amazon.com Inc", GOOG: "Alphabet Inc",
  META: "Meta Platforms", SPY: "SPDR S&P 500 ETF", QQQ: "Invesco QQQ Trust",
  AMD: "Advanced Micro Devices",
};

$detailClose.addEventListener("click", closeDetailPanel);
$detailBackdrop.addEventListener("click", closeDetailPanel);
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && detailSymbol) { closeDetailPanel(); }
});

// Event delegation for card clicks
$cardGrid.addEventListener("click", function (e) {
  var card = e.target.closest(".candidate-card");
  if (card && card.dataset.symbol) {
    openDetailPanel(card.dataset.symbol);
  }
});

function renderDetailPanel(data) {
  // Header
  document.getElementById("dp-symbol").textContent = data.symbol;
  document.getElementById("dp-company").textContent = data.company_name || data.symbol;

  const chgEl = document.getElementById("dp-change");
  const pct = data.change_pct || 0;
  chgEl.textContent = formatPct(pct);
  chgEl.className = "dp-change " + (pct >= 0 ? "positive" : "negative");

  // Price + bid/ask
  document.getElementById("dp-price").textContent = formatPrice(data.price);
  document.getElementById("dp-bid").textContent = data.bid > 0 ? formatPrice(data.bid) : "\u2014";
  document.getElementById("dp-ask").textContent = data.ask > 0 ? formatPrice(data.ask) : "\u2014";
  document.getElementById("dp-bid-size").textContent = data.bid_size > 0 ? formatNum(data.bid_size) + " shares" : "";
  document.getElementById("dp-ask-size").textContent = data.ask_size > 0 ? formatNum(data.ask_size) + " shares" : "";

  // Short Score
  const score = data.score || 0;
  const scoreEl = document.getElementById("dp-score");
  scoreEl.textContent = score + "/100";
  scoreEl.className = "dp-score-value " + (score >= 80 ? "high" : score >= 60 ? "moderate" : "low");

  const fillEl = document.getElementById("dp-score-fill");
  fillEl.style.width = score + "%";
  fillEl.className = "dp-score-fill " + (score >= 80 ? "high" : score >= 60 ? "moderate" : "low");

  const scoreText = score >= 80 ? "\ud83d\udd25 High short potential \u2014 Multiple red flags detected" :
    score >= 60 ? "\u26a0\ufe0f Moderate risk \u2014 Monitor closely" : "Low risk profile";
  document.getElementById("dp-score-text").textContent = scoreText;

  // RSI
  const tech = data.technicals || {};
  const rsi = tech.rsi_14 || 50;
  document.getElementById("dp-rsi-value").textContent = Math.round(rsi);
  document.getElementById("dp-rsi-marker").style.left = rsi + "%";

  // Fundamentals
  renderFundamentals(data.fundamentals || {});

  // Sentiment
  renderSentiment(data.sentiment || {});

  // News
  renderNewsFeed(data.news || []);

  // Chart
  renderPriceChart(data.candles || []);
}

function renderFundamentals(fund) {
  function fmtCompact(n) {
    if (!n || n === 0) {return "\u2014";}
    if (Math.abs(n) >= 1e12) {return "$" + (n / 1e12).toFixed(2) + "T";}
    if (Math.abs(n) >= 1e9) {return "$" + (n / 1e9).toFixed(2) + "B";}
    if (Math.abs(n) >= 1e6) {return "$" + (n / 1e6).toFixed(1) + "M";}
    return "$" + Number(n).toLocaleString();
  }
  const items = [
    { label: "Market Cap", value: fmtCompact(fund.market_cap), warn: false },
    { label: "Revenue", value: fmtCompact(fund.revenue), warn: false },
    { label: "EBITDA", value: fmtCompact(fund.ebitda), warn: fund.ebitda < 0 },
    { label: "Total Debt", value: fmtCompact(fund.total_debt), warn: fund.total_debt > (fund.total_cash || 1) * 3 },
    { label: "Cash", value: fmtCompact(fund.total_cash), warn: false },
    { label: "P/E Ratio", value: fund.pe_ratio ? fund.pe_ratio.toFixed(1) + "x" : "\u2014", warn: fund.pe_ratio > 100 },
    { label: "Beta", value: fund.beta ? fund.beta.toFixed(2) : "\u2014", warn: fund.beta > 2 },
    { label: "Short Ratio", value: fund.short_ratio ? fund.short_ratio.toFixed(1) : "\u2014", warn: fund.short_ratio > 5 },
  ];

  const html = items.map(function (it) {
    return '<div class="dp-fund-item' + (it.warn ? ' warning' : '') + '">' +
      '<div class="dp-fund-label">' + escapeHtml(it.label) + '</div>' +
      '<div class="dp-fund-value">' + escapeHtml(String(it.value)) + '</div>' +
      '</div>';
  }).join("");
  document.getElementById("dp-fund-grid").innerHTML = html;
}

function renderSentiment(sent) {
  const channels = [
    { name: "Twitter/X", key: "twitter", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="dp-sent-icon"><path d="M22 4s-.7 2.1-2 3.4c1.6 10-9.4 17.3-18 11.6 2.2.1 4.4-.6 6-2C3 15.5.5 9.6 3 5c2.2 2.6 5.6 4.1 9 4-.9-4.2 4-6.6 7-3.8 1.1 0 3-1.2 3-1.2z"/></svg>' },
    { name: "Reddit", key: "reddit", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="dp-sent-icon"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>' },
    { name: "Discord", key: "discord", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="dp-sent-icon"><line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/></svg>' },
    { name: "News", key: "news", icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="dp-sent-icon"><path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/></svg>' },
  ];

  const html = channels.map(function (ch) {
    const val = sent[ch.key] || 50;
    const cls = val >= 60 ? "bullish" : val <= 40 ? "bearish" : "mixed";
    return '<div class="dp-sent-row">' +
      ch.icon +
      '<span class="dp-sent-name">' + ch.name + '</span>' +
      '<div class="dp-sent-track"><div class="dp-sent-fill ' + cls + '" style="width: ' + val + '%"></div></div>' +
      '<span class="dp-sent-val">' + val + '</span>' +
      '</div>';
  }).join("");

  // Overall sentiment header
  const overall = sent.overall || 50;
  const overallLabel = sent.overall_label || "Mixed";
  const overallCls = overall >= 60 ? "bullish" : overall <= 40 ? "bearish" : "mixed";
  const header = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;padding:8px 12px;background:var(--bg-card);border-radius:8px;border:1px solid var(--border-card)">' +
    '<span style="font-size:13px;color:var(--text-secondary)">Overall AI Score</span>' +
    '<span style="font-family:JetBrains Mono,monospace;font-size:18px;font-weight:800" class="dp-sent-fill ' + overallCls + '" style="background:none">' + overall + '/100 <span class="dp-news-sent ' + overallCls + '">' + escapeHtml(overallLabel) + '</span></span>' +
    '</div>';

  document.getElementById("dp-sentiment-bars").innerHTML = header + html;
}

function renderNewsFeed(articles) {
  if (!articles || articles.length === 0) {
    document.getElementById("dp-news-feed").innerHTML = '<div class="dp-loading">No recent news available</div>';
    return;
  }

  const html = articles.map(function (art) {
    const sent = art.sentiment || "neutral";
    const tag = art.url ? 'a href="' + escapeHtml(art.url) + '" target="_blank" rel="noopener noreferrer"' : 'div';
    const closeTag = art.url ? 'a' : 'div';
    return '<' + tag + ' class="dp-news-item ' + sent + '">' +
      '<div class="dp-news-meta">' +
        '<span class="dp-news-source">' + escapeHtml(art.source || "News") + '</span>' +
        '<span>\u2022 ' + escapeHtml(art.timestamp || "") + '</span>' +
        '<span class="dp-news-sent ' + sent + '">' + escapeHtml(sent) + '</span>' +
      '</div>' +
      '<div class="dp-news-headline">' + escapeHtml(art.headline || "") + '</div>' +
      '</' + closeTag + '>';
  }).join("");

  document.getElementById("dp-news-feed").innerHTML = html;
}

function renderPriceChart(candles) {
  const canvas = document.getElementById("dp-chart");
  const ctx = canvas.getContext("2d");
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width - 24;
  canvas.height = rect.height - 24;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  if (!candles || candles.length < 2) {
    ctx.fillStyle = "#64748b";
    ctx.font = "13px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("No intraday data available", canvas.width / 2, canvas.height / 2);
    return;
  }

  const prices = candles.map(function (c) { return c.close; });
  const minP = Math.min.apply(null, prices);
  const maxP = Math.max.apply(null, prices);
  const range = maxP - minP || 1;
  const w = canvas.width;
  const h = canvas.height;
  const padTop = 10;
  const padBottom = 20;
  const padLeft = 50;
  const plotW = w - padLeft - 10;
  const plotH = h - padTop - padBottom;

  // Draw Y-axis labels
  ctx.fillStyle = "#64748b";
  ctx.font = "10px JetBrains Mono, monospace";
  ctx.textAlign = "right";
  for (var yi = 0; yi <= 4; yi++) {
    var yVal = minP + range * (1 - yi / 4);
    var yPos = padTop + plotH * (yi / 4);
    ctx.fillText("$" + yVal.toFixed(2), padLeft - 6, yPos + 3);
    // Grid line
    ctx.strokeStyle = "rgba(255,255,255,0.04)";
    ctx.beginPath();
    ctx.moveTo(padLeft, yPos);
    ctx.lineTo(w - 10, yPos);
    ctx.stroke();
  }

  // Price line
  var isUp = prices[prices.length - 1] >= prices[0];
  var lineColor = isUp ? "#34d399" : "#f87171";

  ctx.beginPath();
  for (var i = 0; i < prices.length; i++) {
    var x = padLeft + (i / (prices.length - 1)) * plotW;
    var y = padTop + (1 - (prices[i] - minP) / range) * plotH;
    if (i === 0) {ctx.moveTo(x, y);} else {ctx.lineTo(x, y);}
  }
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 2;
  ctx.stroke();

  // Gradient fill
  var grad = ctx.createLinearGradient(0, padTop, 0, h - padBottom);
  grad.addColorStop(0, isUp ? "rgba(52,211,153,0.25)" : "rgba(248,113,113,0.25)");
  grad.addColorStop(1, "rgba(0,0,0,0)");

  ctx.lineTo(padLeft + plotW, h - padBottom);
  ctx.lineTo(padLeft, h - padBottom);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // X-axis time labels
  ctx.fillStyle = "#64748b";
  ctx.font = "10px Inter, sans-serif";
  ctx.textAlign = "center";
  var step = Math.max(1, Math.floor(candles.length / 5));
  for (var xi = 0; xi < candles.length; xi += step) {
    var xp = padLeft + (xi / (candles.length - 1)) * plotW;
    var timeStr2 = candles[xi].time || "";
    // Parse ISO to short time
    try {
      var dt = new Date(timeStr2);
      timeStr2 = dt.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    } catch (e) { /* keep raw */ }
    ctx.fillText(timeStr2, xp, h - 4);
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
(function init() {
  renderSkeletonCards();
  fetchData();
  connectWebSocket();
  // Fallback polling in case WS never connects
  pollTimer = setInterval(fetchData, 15000);
})();
