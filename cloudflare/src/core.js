// The chart's maths, ported line for line from nse-chart/live.py so the Worker gives the same
// bias, record and trade plan. Bars are [start, open, high, low, close, volume] with start in
// UTC epoch seconds; every clock rule (9:15 open, 15:10 flat) is read in IST.

export const IST = 19800;          // seconds: UTC+5:30, no daylight saving
export const INDEX = 'NIFTY 50';
export const INDEX_YAHOO = '^NSEI';
const BAR = 300;
const OPEN = 555, OR_END = 570, CLOSE = 930;            // minutes after midnight IST
const FIRST_READ = 585, LAST_READ = 870, FLAT_BY = 910, NO_ENTRIES_AFTER = 870;
const OR_BARS = 3, HORIZON = 6;

const NSE_HOLIDAYS = new Set([
  '2026-01-15', '2026-01-26', '2026-03-03', '2026-03-26', '2026-03-31', '2026-04-03', '2026-04-14',
  '2026-05-01', '2026-05-28', '2026-06-26', '2026-09-14', '2026-10-02', '2026-10-20', '2026-11-10',
  '2026-11-24', '2026-12-25',
]);

// The Nifty 50 on October 30 2026 (niftyindices.com). NSE reviews it twice a year.
export const NIFTY50 = ['ADANIENT', 'ADANIPORTS', 'APOLLOHOSP', 'ASIANPAINT', 'AXISBANK', 'BAJAJ-AUTO',
  'BAJAJFINSV', 'BAJFINANCE', 'BEL', 'BHARTIARTL', 'BSE', 'CIPLA', 'COALINDIA', 'DRREDDY', 'EICHERMOT',
  'ETERNAL', 'GRASIM', 'HCLTECH', 'HDFCBANK', 'HDFCLIFE', 'HINDALCO', 'HINDUNILVR', 'ICICIBANK', 'INDIGO',
  'INFY', 'ITC', 'JIOFIN', 'JSWSTEEL', 'KOTAKBANK', 'LT', 'M&M', 'MARUTI', 'MAXHEALTH', 'NESTLEIND', 'NTPC',
  'ONGC', 'POWERGRID', 'RELIANCE', 'SBILIFE', 'SBIN', 'SHRIRAMFIN', 'SUNPHARMA', 'TATACONSUM', 'TATASTEEL',
  'TCS', 'TECHM', 'TITAN', 'TMPV', 'TRENT', 'ULTRACEMCO'];

export class ChartError extends Error {}

// ---------------------------------------------------------------- IST clock

const pad = (n) => String(n).padStart(2, '0');
const istDate = (ts) => new Date((ts + IST) * 1000);
export const dateOf = (ts) => istDate(ts).toISOString().slice(0, 10);
export const minuteOf = (ts) => { const d = istDate(ts); return d.getUTCHours() * 60 + d.getUTCMinutes(); };
export const hhmm = (ts) => { const d = istDate(ts); return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()); };
export const stamp = (ts) => istDate(ts).toISOString().slice(0, 19).replace('T', ' ');
// epoch seconds of a minute after midnight IST on a date 'YYYY-MM-DD'
export const atIst = (date, minute) => Date.parse(date + 'T00:00:00Z') / 1000 - IST + minute * 60;

export function tradingDay(date) {
  const wd = new Date(date + 'T00:00:00Z').getUTCDay();
  return wd >= 1 && wd <= 5 && !NSE_HOLIDAYS.has(date);
}

export function marketState(now) {
  const m = minuteOf(now);
  if (!tradingDay(dateOf(now))) return ['closed', 'NSE is closed today (weekend or holiday).'];
  if (m < OPEN) return ['pre', 'Before the 9:15 AM open.'];
  if (m >= CLOSE) return ['closed', 'After the 3:30 PM close.'];
  return ['open', 'Market open.'];
}

// When the last session that has ended closed (15:30 IST), in epoch seconds.
export function lastClose(now) {
  for (let back = 0; back < 15; back++) {
    const date = dateOf(now - back * 86400);
    const close = atIst(date, CLOSE);
    if (tradingDay(date) && close <= now) return close;
  }
  return now - 15 * 86400;
}

// ---------------------------------------------------------------- symbols and Yahoo's chart

export function canon(sym) {
  const s = String(sym || INDEX).trim().toUpperCase();
  if ([INDEX, 'NIFTY', 'NIFTY50', INDEX_YAHOO].includes(s)) return INDEX;
  return s.endsWith('.NS') || s.endsWith('.BO') ? s.slice(0, -3) : s;
}
export const yahooSymbol = (sym) => (sym === INDEX ? INDEX_YAHOO : sym + '.NS');

const r2 = (x) => Math.round(x * 100) / 100;

export function parseChart(chart, interval = '5m') {
  const res = chart && chart.chart && chart.chart.result && chart.chart.result[0];
  if (!res) throw new ChartError('Yahoo sent no chart');
  const q = ((res.indicators || {}).quote || [{}])[0] || {};
  const bars = [];
  (res.timestamp || []).forEach((ts, i) => {
    let start = ts;
    if (interval === '1d') start = atIst(dateOf(ts), OPEN);
    else if (ts % 300 || minuteOf(ts) >= CLOSE) return;   // the live tick, or the 15:30 closing print
    const v = ['open', 'high', 'low', 'close', 'volume'].map(k => (q[k] || [])[i]);
    if (v.slice(0, 4).some(x => x == null)) return;
    if (bars.length && bars[bars.length - 1][0] === start) bars.pop();
    bars.push([start, r2(v[0]), r2(v[1]), r2(v[2]), r2(v[3]), Math.trunc(v[4] || 0)]);
  });
  return [bars, res.meta || {}];
}

// ---------------------------------------------------------------- money and rounding

// Python's Decimal(str(x)).quantize(..., ROUND_HALF_UP): round the decimal text, not the binary value.
function halfUp(x, d) {
  const a = Math.abs(x), v = Number(Math.round(Number(a + 'e' + d)) + 'e-' + d);
  return x < 0 ? -v : v;
}
// Python's '%.0f': round half to even on the exact value
function fmt0(x) {
  const f = Math.floor(x);
  if (x - f === 0.5) return String(f % 2 === 0 ? f : f + 1);
  return x.toFixed(0);
}

export function inr(x) {
  const q = halfUp(x, 2), sign = q < 0 ? '-' : '';
  let [whole, frac] = Math.abs(q).toFixed(2).split('.');
  if (whole.length > 3) {
    let head = whole.slice(0, -3);
    const groups = [];
    while (head.length > 2) { groups.unshift(head.slice(-2)); head = head.slice(0, -2); }
    if (head) groups.unshift(head);
    whole = groups.concat([whole.slice(-3)]).join(',');
  }
  return sign + '₹' + whole + '.' + frac;
}

export function tickSize(price, sym) {
  if (sym === INDEX) return 0.05;           // the index moves in 5 paise steps, whatever its level
  if (price < 250) return 0.01;
  if (price < 1000) return 0.05;
  if (price < 5000) return 0.10;
  if (price < 10000) return 0.50;
  if (price < 20000) return 1.00;
  return 5.00;
}

// A float's shortest decimal text (the same as Python's str) as [digits, scale]: 1221.1999 -> [12211999n, 4]
function decimal(x) {
  let s = String(x), e = 0;
  if (s.includes('e')) { e = +s.split('e')[1]; s = s.split('e')[0]; }
  const [w, f = ''] = s.split('.');
  let scale = f.length - e, digits = BigInt(w + f);
  if (scale < 0) { digits *= 10n ** BigInt(-scale); scale = 0; }
  return [digits, scale];
}

// Round onto one tick grid: nearest (half up) or down, in exact decimals like live.py's Decimal(str(price)):
// a midpoint that floats to 1221.1999999999998 rounds down to 1221.1, as it does in nse-exit.
export function snap(price, tick, mode) {
  const [pd, ps] = decimal(price), [td, ts] = decimal(tick);
  const scale = Math.max(ps, ts);
  const P = pd * 10n ** BigInt(scale - ps), T = td * 10n ** BigInt(scale - ts);
  const k = mode === 'down' ? P / T : (2n * P + T) / (2n * T);   // prices are positive: division floors
  return Number(k * T) / 10 ** scale;
}

// ---------------------------------------------------------------- indicators

function ema(values, n) {
  const out = [], k = 2 / (n + 1);
  let e = null;
  for (const v of values) { e = e === null ? v : e + k * (v - e); out.push(e); }
  return out;
}

function rsi(values, n = 14) {
  const out = new Array(values.length).fill(null);
  if (values.length <= n) return out;
  const gains = [], losses = [];
  for (let i = 1; i < values.length; i++) {
    gains.push(Math.max(values[i] - values[i - 1], 0));
    losses.push(Math.max(values[i - 1] - values[i], 0));
  }
  let ag = 0, al = 0;
  for (let i = 0; i < n; i++) { ag += gains[i]; al += losses[i]; }
  ag /= n; al /= n;
  for (let i = n; i < values.length; i++) {
    if (i > n) { ag = (ag * (n - 1) + gains[i - 1]) / n; al = (al * (n - 1) + losses[i - 1]) / n; }
    out[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  }
  return out;
}

export const closedBars = (bars, now) => bars.filter(b => b[0] + BAR <= now);

export class Series {
  constructor(bars) {
    this.bars = bars;
    const closes = bars.map(b => b[4]);
    this.ema9 = ema(closes, 9); this.ema21 = ema(closes, 21); this.rsi = rsi(closes);
    this.dayStart = []; this.prevClose = []; this.orHL = []; this.vwap = []; this.date = [];
    let start = 0, prev = null, lastClose = null, rng = null, pv = 0, vol = 0;
    bars.forEach((b, i) => {
      const d = dateOf(b[0]);
      this.date.push(d);
      if (i === 0 || d !== this.date[i - 1]) { start = i; prev = lastClose; rng = null; pv = 0; vol = 0; }
      if (rng === null && i - start + 1 === OR_BARS && minuteOf(bars[start][0]) === OPEN) {
        const part = bars.slice(start, i + 1);
        rng = [Math.max(...part.map(x => x[2])), Math.min(...part.map(x => x[3]))];
      }
      pv += (b[2] + b[3] + b[4]) / 3 * b[5];
      vol += b[5];
      this.dayStart.push(start); this.prevClose.push(prev); this.orHL.push(rng);
      this.vwap.push(vol ? pv / vol : null);
      lastClose = b[4];
    });
  }
}

export function label(score) {
  if (score >= 3) return 'Bullish';
  if (score <= -3) return 'Bearish';
  if (score > 0) return 'Leaning bullish';
  if (score < 0) return 'Leaning bearish';
  return 'Neutral';
}

const sgn = (a, b) => (a > b ? 1 : a < b ? -1 : 0);

// The five votes at closed bar i. With words=false only the score (the backtest reads 3,000+ bars).
export function readAt(s, i, breadth = null, words = true) {
  const b = s.bars[i], c = b[4], sig = [];
  const add = (name, vote, detail) => sig.push({ name, vote, detail: words ? detail() : '' });
  const rng = s.orHL[i];
  if (rng === null) add('Opening range', 0, () => 'Still forming (9:15 to 9:30)');
  else if (c > rng[0]) add('Opening range', 1, () => 'Above the 9:15–9:30 high ' + inr(rng[0]));
  else if (c < rng[1]) add('Opening range', -1, () => 'Below the 9:15–9:30 low ' + inr(rng[1]));
  else add('Opening range', 0, () => 'Inside the range ' + inr(rng[1]) + ' to ' + inr(rng[0]));
  const e9 = s.ema9[i], e21 = s.ema21[i];
  add('Trend (EMA 9 vs 21)', sgn(e9, e21), () => 'EMA 9 ' + inr(e9) + ' ' + (e9 >= e21 ? 'above' : 'below') + ' EMA 21 ' + inr(e21));
  const pc = s.prevClose[i];
  if (pc) {
    const ch = (c - pc) / pc * 100;
    add('Vs previous close', sgn(ch, 0), () => (ch >= 0 ? '+' : '') + ch.toFixed(2) + ' % vs ' + inr(pc));
  } else add('Vs previous close', 0, () => 'No previous session in the data');
  const r = s.rsi[i];
  if (r === null) add('Momentum (RSI 14)', 0, () => 'Not enough bars yet');
  else add('Momentum (RSI 14)', r > 55 ? 1 : r < 45 ? -1 : 0, () => fmt0(r) + ' (' + (r > 55 ? 'over 55' : r < 45 ? 'under 45' : '45 to 55') + ')');
  if (s.vwap[i] !== null) {
    const v = s.vwap[i];
    add('VWAP', sgn(c, v), () => (c >= v ? 'Above' : 'Below') + ' VWAP ' + inr(v));
  } else if (breadth !== null && breadth !== undefined) {
    add('Breadth', breadth >= 0.6 ? 1 : breadth <= 0.4 ? -1 : 0, () => fmt0(breadth * 100) + ' % of Nifty 50 stocks above their VWAP');
  }
  const score = sig.reduce((a, x) => a + x.vote, 0);
  return { score, max: sig.length, label: label(score), signals: sig, at: hhmm(b[0] + BAR), close: c };
}

export function backtest(bars) {
  const s = new Series(bars);
  const out = { sessions: new Set(s.date).size, bars: 0, base_up: 0,
    bull: { calls: 0, right30: 0, right_flat: 0, days: new Set() },
    bear: { calls: 0, right30: 0, right_flat: 0, days: new Set() } };
  const flatClose = {};
  bars.forEach((b, i) => { if (minuteOf(b[0]) < FLAT_BY) flatClose[s.date[i]] = b[4]; });
  bars.forEach((b, i) => {
    const end = minuteOf(b[0] + BAR);
    if (!(FIRST_READ <= end && end <= LAST_READ) || s.prevClose[i] === null) return;
    const j = i + HORIZON;
    if (j >= bars.length || s.date[j] !== s.date[i]) return;
    const later = bars[j][4], c = b[4];
    out.bars += 1;
    out.base_up += later > c ? 1 : 0;
    const score = readAt(s, i, null, false).score;
    const side = score >= 3 ? 'bull' : score <= -3 ? 'bear' : null;
    if (!side) return;
    const k = out[side], up = side === 'bull';
    k.calls += 1;
    k.days.add(s.date[i]);
    k.right30 += (up ? later > c : later < c) ? 1 : 0;
    const fc = flatClose[s.date[i]];
    if (fc !== undefined) k.right_flat += (up ? fc > c : fc < c) ? 1 : 0;
  });
  out.bull.days = out.bull.days.size;
  out.bear.days = out.bear.days.size;
  return out;
}

export function describeBacktest(bt) {
  const pct = (a, n) => (n ? fmt0(100 * a / n) + ' %' : '–');
  const lines = [bt.sessions + ' sessions, ' + bt.bars + ' readings from 9:45 AM to 2:30 PM.',
    'Any time: the close 30 minutes later was higher ' + pct(bt.base_up, bt.bars) + ' of the time.'];
  for (const [side, word, dir] of [['bull', 'Bullish', 'higher'], ['bear', 'Bearish', 'lower']]) {
    const k = bt[side];
    lines.push(word + ' (' + k.calls + ' readings on ' + k.days + ' days): ' + dir + ' 30 minutes later ' +
      pct(k.right30, k.calls) + ', ' + dir + ' at 15:10 ' + pct(k.right_flat, k.calls) + '.');
  }
  return lines;
}

// 5-minute bars -> 15m, 1h (buckets from 9:15, like NSE's session) or 1d candles
export function resample(bars, tf) {
  if (tf === '5m') return bars.map(b => b.slice());
  const size = { '15m': 15, '1h': 60 }[tf];
  const out = [];
  for (const b of bars) {
    const day = atIst(dateOf(b[0]), OPEN);
    const key = size ? day + Math.floor((b[0] - day) / 60 / size) * size * 60 : day;
    const c = out[out.length - 1];
    if (c && c[0] === key) {
      c[2] = Math.max(c[2], b[2]); c[3] = Math.min(c[3], b[3]); c[4] = b[4]; c[5] += b[5];
    } else out.push([key, b[1], b[2], b[3], b[4], b[5]]);
  }
  return out;
}

// ---------------------------------------------------------------- the trade plan (nse-entry, nse-exit, nse-paper)

// The day's levels, both sides, as orb_plan in live.py: BUY on a 5-minute close above the opening-range high
// (SL the range low), SELL on a close below the low (SL the high), T1-T3 at 1-3 range widths past the entry.
export function orbPlan(sym, today, now) {
  const bars = today.filter(b => b[0] + BAR <= now);
  const rng = bars.filter(b => minuteOf(b[0]) >= OPEN && minuteOf(b[0]) < OR_END);
  if (rng.length < OR_BARS) return { status: 'forming' };
  const hi = Math.max(...rng.map(b => b[2])), lo = Math.min(...rng.map(b => b[3]));
  const tick = tickSize(hi, sym), width = r2(hi - lo);
  const out = { status: 'ready', or_high: hi, or_low: lo, width, tick };
  if (width < 5 * tick - 1e-9) return Object.assign(out, { status: 'refused', why: 'opening range under 5 ticks wide: no levels today' });
  const last = bars[bars.length - 1];
  const late = minuteOf(last[0] + BAR) >= NO_ENTRIES_AFTER && dateOf(last[0] + BAR) === dateOf(last[0]);
  for (const [side, entry, stop, k] of [['buy', hi, lo, 1], ['sell', lo, hi, -1]]) {
    const s = { entry, stop, risk: width, targets: [1, 2, 3].map(n => snap(r2(entry + k * n * width), tick, 'nearest')),
      status: late ? 'none' : 'waiting', hits: [], exit: null };
    out[side] = s;
    let trig = null;
    for (let i = 0; i < bars.length; i++) {
      const b = bars[i];
      if (minuteOf(b[0]) < OR_END) continue;
      if (minuteOf(b[0] + BAR) > NO_ENTRIES_AFTER) break;
      if (k > 0 ? b[4] > hi : b[4] < lo) { trig = i; break; }
    }
    if (trig === null) continue;
    Object.assign(s, { status: 'triggered', entry_time: hhmm(bars[trig][0] + BAR), fill: bars[trig][4] });
    for (const b of bars.slice(trig + 1)) {
      const end = hhmm(b[0] + BAR);
      if (minuteOf(b[0]) >= FLAT_BY) { s.exit = { how: 'flat', price: b[1], time: hhmm(b[0]) }; break; }
      if (k > 0 ? b[3] <= stop : b[2] >= stop) { s.exit = { how: 'stop', price: k > 0 ? Math.min(stop, b[1]) : Math.max(stop, b[1]), time: end }; break; }
      while (s.hits.length < 3 && (k > 0 ? b[2] >= s.targets[s.hits.length] : b[3] <= s.targets[s.hits.length])) s.hits.push(end);
      if (s.hits.length === 3) { s.exit = { how: 't3', price: s.targets[2], time: end }; break; }
    }
  }
  return out;
}

// The last closed 5-minute bar of today against the usual volume for that time of day (volume_surge in
// chart_signals.py): the median of the same slot on up to 20 earlier sessions, at least 3. Big move likely,
// direction not known.
export function volumeSurge(bars, now, times = 3, days = 20) {
  const done = bars.filter(b => b[0] + BAR <= now);
  if (!done.length) return null;
  const last = done[done.length - 1], d = dateOf(last[0]), slot = minuteOf(last[0]);
  if (d !== dateOf(now)) return null;
  const hist = done.slice(0, -1).filter(b => minuteOf(b[0]) === slot && dateOf(b[0]) < d).map(b => b[5]).slice(-days);
  if (hist.length < 3 || !(last[5] > 0)) return null;
  const usual = hist.slice().sort((x, y) => x - y)[Math.floor(hist.length / 2)];
  if (!(usual > 0)) return null;
  const r = last[5] / usual;
  return r >= times ? { ratio: Math.floor(r * 10 + 0.5) / 10, at: hhmm(last[0] + BAR) } : null;
}

// Today's 5-minute payload: the session's bars, the previous close and the bias read at the last closed bar.
export function payload(sym, bars, meta, now, breadth = null) {
  if (!bars.length) throw new ChartError('No bars for ' + sym);
  const done = closedBars(bars, now);
  const day = dateOf(bars[bars.length - 1][0]);
  const today = bars.filter(b => dateOf(b[0]) === day);
  const prev = bars.filter(b => dateOf(b[0]) < day);
  let read = null;
  if (done.length && dateOf(done[done.length - 1][0]) === day) read = readAt(new Series(done), done.length - 1, breadth);
  return {
    symbol: sym, name: sym === INDEX ? INDEX : (meta.longName || meta.shortName || sym),
    is_index: sym === INDEX, session: day,
    prev_close: prev.length ? prev[prev.length - 1][4] : (meta.chartPreviousClose ?? null),
    bars: today, closed: today.filter(b => b[0] + BAR <= now).length, bias: read,
  };
}
