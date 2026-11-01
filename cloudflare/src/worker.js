// NSE Live Chart on Cloudflare: the nse-chart page and its three API routes (/api/status,
// /api/chart, /api/backtest), with the Nifty 50 list refreshed by a once-a-minute cron into D1.
// Free Yahoo bars, unofficial and can lag. A bias is a description of the chart so far, not a forecast.

import PAGE from '../../nse-chart/page.html';
import LOGIN_PAGE from './login.html';
import { signedIn, authRoute } from './auth.js';
import {
  INDEX, NIFTY50, ChartError, canon, yahooSymbol, parseChart, payload, closedBars, orbPlan,
  backtest, describeBacktest, resample, marketState, lastClose, dateOf, stamp,
} from './core.js';

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';
const YAHOO_HOSTS = ['https://query2.finance.yahoo.com', 'https://query1.finance.yahoo.com'];
const TIMEFRAMES = { '5m': ['5m', '60d'], '15m': ['5m', '60d'], '1h': ['1h', '1y'], '1d': ['1d', '2y'] };
const PARTS = 5;                       // the 50 stocks in 5 parts of 10: one part a minute, each every 5 minutes
const CHART_TTL = 30, CLOSED_TTL = 600, HIST_TTL = 900;

// The page as nse-chart serves it, with the one line that names live.py reworded for the web.
const HTML = PAGE.replace('Not connected: is live.py still running?', 'Not connected: try again in a minute.');

const esc = (x) => String(x).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const nowTs = () => Math.floor(Date.now() / 1000);

// ---------------------------------------------------------------- Yahoo, with a short memory per isolate

const memo = new Map();   // key -> [fetched_at, value]
function remembered(key, ttl) {
  const hit = memo.get(key);
  return hit && nowTs() - hit[0] < ttl ? hit[1] : null;
}
function remember(key, value) {
  if (memo.size > 200) memo.clear();
  memo.set(key, [nowTs(), value]);
  return value;
}

async function yahoo(path) {
  let last = null;
  for (const host of YAHOO_HOSTS) {
    for (const attempt of [0, 1]) {
      let r;
      try {
        r = await fetch(host + path, { headers: { 'User-Agent': UA, Accept: 'application/json' } });
      } catch (e) { last = e.message; break; }
      if (r.ok) return r.json();
      last = r.status;
      if (r.status === 429 && attempt === 0) { await sleep(3000); continue; }
      if (r.status === 429 || r.status >= 500) break;
      throw new ChartError('Yahoo answered ' + r.status + ' for ' + path);
    }
  }
  throw new ChartError('Yahoo did not answer (' + last + '): try again in a minute');
}

async function bars(sym, range, interval, now) {
  const path = '/v8/finance/chart/' + encodeURIComponent(yahooSymbol(sym)) + '?range=' + range + '&interval=' + interval;
  const [b, meta] = parseChart(await yahoo(path), interval);
  return [b.filter(x => x[0] < now), meta];
}

// ---------------------------------------------------------------- D1: the board and the day's records

async function getRows(env, like) {
  const { results } = await env.DB.prepare('SELECT k, t, v FROM kv WHERE k LIKE ?').bind(like).all();
  return results;
}
const put = (env, k, v) => env.DB.prepare('INSERT OR REPLACE INTO kv (k, t, v) VALUES (?, ?, ?)')
  .bind(k, nowTs(), JSON.stringify(v)).run();

async function board(env) {
  const hit = remembered('board', 20);
  if (hit) return hit;
  const rows = await getRows(env, 'board:%');
  const by = Object.fromEntries(rows.map(r => [r.k, r]));
  const stocks = [], failed = [];
  let updated = 0, above = 0, counted = 0;
  for (let p = 0; p < PARTS; p++) {
    const r = by['board:' + p];
    if (!r) continue;
    const v = JSON.parse(r.v);
    stocks.push(...v.rows); failed.push(...v.failed);
    updated = Math.max(updated, r.t);
  }
  for (const s of stocks) if (s.vwap_vote) { counted += 1; above += s.vwap_vote > 0 ? 1 : 0; }
  stocks.sort((a, b) => NIFTY50.indexOf(a.symbol) - NIFTY50.indexOf(b.symbol));
  const idx = by['board:index'] ? JSON.parse(by['board:index'].v) : null;
  return remember('board', {
    stocks, failed, error: null, breadth: counted ? above / counted : null,
    updated: updated ? stamp(updated).slice(11) : null,
    // board:index was today's bars alone before it carried the index price; that shape still reads, without a price
    index_today: Array.isArray(idx) ? idx : idx && idx.today,
    index_last: idx && !Array.isArray(idx) ? idx.last : null,
    index_prev_close: idx && !Array.isArray(idx) ? idx.prev_close : null,
  });
}

// One part of the list: 10 stocks (part 0 also the index), each with its read and the default plan.
async function refreshPart(env, part, now) {
  const syms = NIFTY50.filter((_, i) => i % PARTS === part);
  const rows = [], failed = [];
  for (const sym of syms) {
    try {
      const [b, meta] = await bars(sym, '5d', '5m', now);
      const p = payload(sym, b, meta, now);
      const v = p.bias && p.bias.signals.find(x => x.name === 'VWAP');
      const today = p.session === dateOf(now) ? p.bars : null;
      rows.push({ symbol: sym, last: p.bars.length ? p.bars[p.bars.length - 1][4] : null, prev_close: p.prev_close,
        score: p.bias ? p.bias.score : null, label: p.bias ? p.bias.label : null,
        plan: today ? orbPlan(sym, today, now) : null, vwap_vote: v ? v.vote : 0 });
    } catch (e) {
      failed.push(sym);
    }
    await sleep(300);              // be polite to a free endpoint
  }
  await put(env, 'board:' + part, { rows, failed });
  if (part === 0) {
    try {
      const [b] = await bars(INDEX, '5d', '5m', now);
      const day = b.length ? dateOf(b[b.length - 1][0]) : null, prev = b.filter(x => dateOf(x[0]) < day);
      await put(env, 'board:index', {
        today: day === dateOf(now) ? b.filter(x => dateOf(x[0]) === day) : null,
        last: b.length ? b[b.length - 1][4] : null, prev_close: prev.length ? prev[prev.length - 1][4] : null,
      });
    } catch (e) { /* the index plan waits for the next round */ }
  }
}

async function cron(env) {
  const now = nowTs();
  const part = Math.floor(now / 60) % PARTS;
  if (marketState(now)[0] !== 'open') {
    // Closed: refresh only a part that was last read before the last session ended (fills it once after 15:30).
    const [row] = (await getRows(env, 'board:' + part));
    if (row && row.t >= lastClose(now) + 60) return;
  }
  await refreshPart(env, part, now);
}

// ---------------------------------------------------------------- the API

async function chart(env, sym, tf, now) {
  if (!TIMEFRAMES[tf]) throw new ChartError('Timeframe ' + tf + ': use 5m, 15m, 1h or 1d');
  const ttl = marketState(now)[0] === 'open' ? CHART_TTL : CLOSED_TTL;
  const key = 'chart|' + sym + '|' + tf;
  const hit = remembered(key, ttl);
  if (hit) return hit;
  const breadth = sym === INDEX ? (await board(env)).breadth : null;
  const [interval, range] = TIMEFRAMES[tf];
  let p, candles;
  if (interval === '5m') {
    // One fetch: 60 sessions of 5-minute bars give both today's read and the earlier candles.
    const [b, meta] = await bars(sym, '60d', '5m', now);
    p = payload(sym, b, meta, now, breadth);
    candles = resample(b, tf);
  } else {
    const [b, meta] = await bars(sym, '5d', '5m', now);
    p = payload(sym, b, meta, now, breadth);
    let hist = remembered('hist|' + sym + '|' + tf, HIST_TTL);
    if (!hist) hist = remember('hist|' + sym + '|' + tf, (await bars(sym, range, interval, now))[0]);
    candles = hist.filter(c => dateOf(c[0]) < p.session).concat(resample(p.bars, tf));
  }
  return remember(key, Object.assign(p, { tf, candles }));
}

async function record(env, sym, now) {
  const key = 'bt:' + sym + ':' + dateOf(now);
  const hit = remembered(key, 86400);
  if (hit) return hit;
  const row = await env.DB.prepare('SELECT v FROM kv WHERE k = ?').bind(key).first();
  if (row) return remember(key, JSON.parse(row.v));
  const [b] = await bars(sym, '60d', '5m', now);
  const bt = backtest(closedBars(b, now));
  bt.lines = describeBacktest(bt);
  await env.DB.prepare("DELETE FROM kv WHERE k LIKE ? AND k <> ?").bind('bt:' + sym + ':%', key).run();
  await put(env, key, bt);
  return remember(key, bt);
}

async function status(env, now) {
  const [state, words] = marketState(now);
  const b = Object.assign({}, await board(env));
  const today = b.index_today;
  delete b.index_today;
  b.index_plan = today && today.length && dateOf(today[0][0]) === dateOf(now) ? orbPlan(INDEX, today, now) : null;
  return { now: stamp(now), market: state, words, offline: false, board: b };
}

const json = (code, body) => new Response(JSON.stringify(body), {
  status: code, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
});

// ---------------------------------------------------------------- the lock
// Our own sign-in (auth.js): an invited email gets a one-time code, the right code a signed session cookie.
// No session, no chart and no data: the page answers with the sign-in screen, the API with 401.

// The brand: candlesticks on the chart's dark panel, at /brand/logo.svg (public: the sign-in page shows it).
const LOGO = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">
<rect width="96" height="96" rx="22" fill="#161a25"/><rect x="1" y="1" width="94" height="94" rx="21" fill="none" stroke="#262b3a" stroke-width="2"/>
<path d="M14 70 L32 56 L46 62 L62 38 L82 26" fill="none" stroke="#5b8cff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" opacity=".55"/>
<g stroke-linecap="round" stroke-width="3"><line x1="26" y1="44" x2="26" y2="78" stroke="#f7525f"/><line x1="48" y1="36" x2="48" y2="74" stroke="#22ab94"/><line x1="70" y1="18" x2="70" y2="60" stroke="#22ab94"/></g>
<rect x="20" y="50" width="12" height="20" rx="2.5" fill="#f7525f"/><rect x="42" y="44" width="12" height="22" rx="2.5" fill="#22ab94"/><rect x="64" y="26" width="12" height="26" rx="2.5" fill="#22ab94"/>
</svg>`;

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === '/brand/logo.svg') {
      return new Response(LOGO, { headers: { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'public, max-age=86400' } });
    }
    if (path.startsWith('/auth/')) return authRoute(request, env, path);
    if (path === '/welcome') return Response.redirect(new URL('/', request.url).toString(), 301);   // the old front door
    const email = await signedIn(request, env);
    if (!email) {
      if (path.startsWith('/api/')) return json(401, { error: 'Sign in first.' });
      if (path !== '/') return Response.redirect(new URL('/', request.url).toString(), 302);
      return new Response(LOGIN_PAGE, { headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store',
        'X-Frame-Options': 'DENY', 'Referrer-Policy': 'same-origin' } });
    }
    const url = new URL(request.url), now = nowTs();
    const sym = canon(url.searchParams.get('sym')), tf = url.searchParams.get('tf') || '5m';
    try {
      if (url.pathname === '/') {
        const out = '/auth/logout';
        const page = HTML
          .replace('<details class="acct" id="acct" hidden>', '<details class="acct" id="acct">')
          .replace('<span class="av" id="av">?</span>', '<span class="av" id="av">' + esc(email.charAt(0)) + '</span>')
          .replace('<div class="who" id="who"></div>', '<div class="who" id="who" title="' + esc(email) + '">' + esc(email) + '</div>')
          .replace('id="signOut" href="#"', 'id="signOut" href="' + out + '"');
        return new Response(page, { headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'same-origin' } });
      }
      if (url.pathname === '/api/status') return json(200, await status(env, now));
      if (url.pathname === '/api/chart') return json(200, await chart(env, sym, tf, now));
      if (url.pathname === '/api/backtest') return json(200, await record(env, sym, now));
      return json(404, { error: 'not found' });
    } catch (e) {
      if (e instanceof ChartError) return json(502, { error: e.message });
      return json(500, { error: 'Something went wrong: ' + e.message });
    }
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(cron(env));
  },
};
