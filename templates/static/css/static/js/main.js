/* AI SmartCart – main.js
   Backend API structure (from app.py):
   POST /api/track  → { product: {id, title, asin, url, currency}, analytics: {current_price, lowest_price, highest_price, average_price, recommendation, anomaly_detected, predicted_price_7d, volatility_percent, samples}, history: [{timestamp, price}] }
   GET  /api/history/<id> → same structure
   GET  /api/health → status
*/

let resultChart = null;
let isDark = true;

const SIGNAL_META = {
  'BUY NOW':    { emoji: '🎯', reason: 'Current price equals the historical lowest — optimal time to buy!' },
  'GOOD DEAL':  { emoji: '💎', reason: 'Current price is below the historical average — a worthwhile purchase.' },
  'OVERPRICED': { emoji: '⚠️', reason: 'Price exceeds 115% of the historical average — consider waiting.' },
  'WAIT':       { emoji: '⏳', reason: 'Price is within normal range — no strong signal either way.' },
};

// ── Theme toggle ────────────────────────────────────────────────────────────
function toggleTheme() {
  isDark = !isDark;
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  document.getElementById('toggleEmoji').textContent = isDark ? '☀️' : '🌙';
  if (resultChart) {
    updateChartTheme();
  }
}

function updateChartTheme() {
  const tickColor = isDark ? '#718096' : '#A0AEC0';
  const gridColor = isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.05)';
  resultChart.options.scales.x.ticks.color = tickColor;
  resultChart.options.scales.y.ticks.color = tickColor;
  resultChart.options.scales.x.grid.color  = gridColor;
  resultChart.options.scales.y.grid.color  = gridColor;
  resultChart.update();
}

// ── Helpers ─────────────────────────────────────────────────────────────────
function fmt(val, currency) {
  if (val === null || val === undefined) return '—';
  return (currency || '') + parseFloat(val).toFixed(2);
}

function signalClass(signal) {
  return (signal || 'wait').toLowerCase().replace(/\s+/g, '-');
}

function setLoading(on) {
  const text   = document.getElementById('trackBtnText');
  const loader = document.getElementById('trackBtnLoader');
  const btn    = document.getElementById('trackBtn');
  if (on) { text.classList.add('hidden'); loader.classList.remove('hidden'); btn.disabled = true; }
  else    { text.classList.remove('hidden'); loader.classList.add('hidden'); btn.disabled = false; }
}

function showError(msg) {
  const el = document.getElementById('trackError');
  el.textContent = msg;
  el.classList.remove('hidden');
}

function clearError() {
  const el = document.getElementById('trackError');
  el.textContent = '';
  el.classList.add('hidden');
}

// ── Render result from API response ─────────────────────────────────────────
function renderResult(data) {
  const product   = data.product   || {};
  const analytics = data.analytics || {};
  const history   = data.history   || [];

  const currency     = product.currency || '₹';
  const signal       = analytics.recommendation || 'WAIT';
  const meta         = SIGNAL_META[signal] || SIGNAL_META['WAIT'];
  const currentPrice = analytics.current_price   || 0;
  const lowestPrice  = analytics.lowest_price    || 0;
  const highestPrice = analytics.highest_price   || 0;
  const avgPrice     = analytics.average_price   || 0;
  const predicted    = analytics.predicted_price_7d || null;
  const isAnomaly    = analytics.anomaly_detected || false;
  const volatility   = analytics.volatility_percent || 0;

  // Signal card
  const card = document.getElementById('signalCard');
  card.className = 'signal-card animate-in ' + signalClass(signal);
  document.getElementById('signalEmoji').textContent = meta.emoji;
  document.getElementById('signalName').textContent  = signal;
  document.getElementById('resultTitle').textContent = product.title || '—';
  document.getElementById('signalReason').textContent = meta.reason;

  // Stats
  document.getElementById('resultStats').innerHTML = `
    <div class="stat-item"><div class="stat-item-label">NOW</div><div class="stat-item-value now">${fmt(currentPrice, currency)}</div></div>
    <div class="stat-item"><div class="stat-item-label">LOWEST</div><div class="stat-item-value low">${fmt(lowestPrice, currency)}</div></div>
    <div class="stat-item"><div class="stat-item-label">AVERAGE</div><div class="stat-item-value avg">${fmt(avgPrice, currency)}</div></div>
    <div class="stat-item"><div class="stat-item-label">HIGHEST</div><div class="stat-item-value high">${fmt(highestPrice, currency)}</div></div>
  `;

  // Anomaly banner
  const banner = document.getElementById('anomalyBanner');
  if (isAnomaly) {
    banner.classList.remove('hidden');
    banner.style.animation = 'none';
    void banner.offsetWidth;
    banner.style.animation = 'anomaly-slide 0.4s ease forwards';
  } else {
    banner.classList.add('hidden');
  }

  // Chart
  renderChart(history, predicted, currentPrice, currency);

  // Show result section
  document.getElementById('resultSection').classList.remove('hidden');
  document.getElementById('resultSection').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ── Chart ────────────────────────────────────────────────────────────────────
function renderChart(history, predicted, currentPrice, currency) {
  const tickColor = isDark ? '#718096' : '#A0AEC0';
  const gridColor = isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.05)';
  const tooltipBg = isDark ? '#1C2333' : '#FFFFFF';
  const tooltipTxt= isDark ? '#E2E8F0' : '#1A202C';

  const histLabels = history.map(h => (h.timestamp || '').slice(0, 10));
  const histPrices = history.map(h => parseFloat(h.price));

  // Forecast: bridge from last history point to predicted
  const foreLabels = predicted ? ['7d forecast'] : [];
  const foreData   = predicted
    ? [...new Array(Math.max(histLabels.length - 1, 0)).fill(null),
       histPrices.length ? histPrices[histPrices.length - 1] : currentPrice,
       predicted]
    : [];

  const allLabels = [...histLabels, ...foreLabels];
  const histData  = [...histPrices, ...new Array(foreLabels.length).fill(null)];

  if (resultChart) resultChart.destroy();

  resultChart = new Chart(document.getElementById('resultChart'), {
    type: 'line',
    data: {
      labels: allLabels,
      datasets: [
        {
          label: 'Historical',
          data: histData,
          borderColor: '#F5A623',
          backgroundColor: 'rgba(245,166,35,0.07)',
          tension: 0.35, fill: true, pointRadius: 2, pointHoverRadius: 5,
        },
        {
          label: '7-Day Forecast',
          data: foreData,
          borderColor: '#4FD1C5',
          borderDash: [5, 3],
          tension: 0.35, fill: false, pointRadius: 3,
          pointBackgroundColor: '#4FD1C5', pointHoverRadius: 5,
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 600, easing: 'easeInOutQuart' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: tooltipBg, titleColor: tooltipTxt,
          bodyColor: tickColor, borderColor: isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.08)',
          borderWidth: 1,
          callbacks: { label: ctx => ctx.raw !== null ? ` ${currency}${parseFloat(ctx.raw).toFixed(2)}` : '' }
        }
      },
      scales: {
        x: { ticks: { color: tickColor, font: { family: 'Space Mono', size: 10 }, maxTicksLimit: 10 }, grid: { color: gridColor } },
        y: { ticks: { color: tickColor, font: { family: 'Space Mono', size: 10 }, callback: v => `${currency}${v.toFixed(0)}` }, grid: { color: gridColor } }
      }
    }
  });
}

// ── Track new product ────────────────────────────────────────────────────────
async function trackProduct() {
  clearError();
  const url = document.getElementById('urlInput').value.trim();
  if (!url) { showError('Please paste an Amazon product URL.'); return; }
  if (!url.includes('amazon')) { showError('URL must be an Amazon product link.'); return; }

  setLoading(true);
  try {
    const res  = await fetch('/api/track', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    if (!res.ok) { showError(data.error || 'Something went wrong. Try again.'); return; }
    renderResult(data);
    setTimeout(() => location.reload(), 4000);
  } catch (e) {
    showError('Network error. Please try again.');
  } finally {
    setLoading(false);
  }
}

// ── Load existing product by ID ──────────────────────────────────────────────
async function loadProduct(productId) {
  try {
    const res  = await fetch(`/api/history/${productId}`);
    const data = await res.json();
    if (!res.ok) { alert(data.error || 'Could not load product.'); return; }
    renderResult(data);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    alert('Network error loading product.');
  }
}

// ── Event listeners ──────────────────────────────────────────────────────────
document.getElementById('trackBtn').addEventListener('click', trackProduct);
document.getElementById('urlInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') trackProduct();
});
