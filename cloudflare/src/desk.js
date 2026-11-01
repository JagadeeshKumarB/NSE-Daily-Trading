// The trading desk, the same as nse-chart/chart_desk.py (cloudflare/test-desk.mjs keeps them equal): size a
// trade, replay today's paper trades and lock the day, read the mood. Paper only: nothing here places an order.
// The evidence behind the mood is in chart_desk.py's docstring.

import { snap } from './core.js';

const BAR = 300, IST = 19800;
export const DESK_DEFAULTS = {
  budget: 100000, cash_buffer_pct: 10, max_position_pct: 75, max_order_inr: 75000,
  max_loss_per_trade_inr: 500, max_daily_loss_inr: 1500, max_trades_per_day: 3,
  count_charges_in_max_loss: true, no_entries_after: '14:30', flat_by: '15:10', allow_short: false, product: 'MIS',
};
const EDITABLE = ['budget', 'max_loss_per_trade_inr', 'max_daily_loss_inr', 'max_trades_per_day', 'allow_short'];
const CHOP_SHARE = 0.6, CHOP_MIN = 5;

export function settingsFrom(overrides) {
  const s = { ...DESK_DEFAULTS };
  for (const k of Object.keys(DESK_DEFAULTS)) if (overrides && overrides[k] != null) s[k] = overrides[k];
  return s;
}
export function cleanSettings(data) {
  const out = {};
  for (const k of EDITABLE) {
    if (!(k in data)) continue;
    let v = data[k];
    if (k === 'allow_short') { out[k] = !!v; continue; }
    v = Number(v);
    if (!Number.isFinite(v)) throw new Error(k + ' must be a number');
    if (k === 'max_trades_per_day') {
      if (v !== Math.trunc(v) || v < 1 || v > 50) throw new Error('max_trades_per_day must be a whole number from 1 to 50');
    } else if (!(v > 0 && v <= 1e9)) throw new Error(k + ' must be above 0');
    out[k] = v;
  }
  return out;
}

// ---- money: Zerodha's intraday charges, each part rounded half up to the paisa (chart_data.charges)
const C = { brokerage_pct: 0.0003, brokerage_cap: 20.0, stt_intraday_sell: 0.00025, exchange_nse: 0.0000307,
  sebi: 0.000001, gst: 0.18, stamp_intraday_buy: 0.00003 };
// half up on the exact decimal text, like Decimal(str(x)).quantize: 23.564999999999998 -> 23.56, never 23.57
const q2 = (x) => (x > 0 ? snap(x, 0.01, 'nearest') : 0);
const r2 = (x) => Number(x.toFixed(2));
export function charges(b, s) {
  const brokerage = [b, s].filter(v => v > 0).reduce((t, v) => t + Math.min(v * C.brokerage_pct, C.brokerage_cap), 0);
  const stt = s * C.stt_intraday_sell, stamp = b * C.stamp_intraday_buy;
  const exchange = (b + s) * C.exchange_nse, sebi = (b + s) * C.sebi, gst = (brokerage + exchange + sebi) * C.gst;
  const parts = [brokerage, stt, exchange, sebi, gst, stamp, 0].map(q2);
  return r2(parts.reduce((t, v) => t + v, 0));
}
const paise = (x) => Math.round(x * 100);
const sharesFor = (amount, per) => { const u = paise(amount), p = paise(per); return p > 0 && u > 0 ? Math.floor(u / p) : 0; };
export const trip = (side, qty, entry, exit) => side === 'buy' ? charges(qty * entry, qty * exit) : charges(qty * exit, qty * entry);

export function sizeTrade(side, entry, stop, targets, s, realizedToday = 0, factor = 1, inUse = 0) {
  const k = side === 'buy' ? 1 : -1, riskP = (paise(entry) - paise(stop)) * k;
  if (riskP <= 0) return { qty: 0, refusal: 'the stop is on the wrong side of the entry' };
  const risk = riskP / 100, perTrade = Number(s.max_loss_per_trade_inr) * factor;
  const left = Number(s.max_daily_loss_inr) + Math.min(realizedToday, 0), maxLoss = Math.min(perTrade, left);
  const limits = [];
  if (maxLoss <= 0) limits.push(['loss left today', 0]);
  else {
    let q = sharesFor(maxLoss, risk);
    if (s.count_charges_in_max_loss !== false) while (q > 0 && q * riskP + paise(trip(side, q, entry, stop)) > paise(maxLoss)) q--;
    limits.push([maxLoss === perTrade ? 'max loss' : 'loss left today', q]);
  }
  const budget = Number(s.budget);
  limits.push(['position cap', sharesFor(budget * Number(s.max_position_pct) / 100, entry)]);
  limits.push(['max order', sharesFor(Number(s.max_order_inr), entry)]);
  limits.push(['cash', sharesFor(Math.max(budget * (1 - Number(s.cash_buffer_pct) / 100) - inUse, 0), entry)]);
  const qty = Math.min(...limits.map(l => l[1])), bound = limits.find(l => l[1] === qty)[0];
  const out = { side, entry, stop, qty, bound_by: bound, factor, risk_per_share: risk, cost: r2(qty * entry), max_loss_used: r2(maxLoss) };
  if (qty === 0) {
    out.refusal = bound === 'loss left today' && maxLoss <= 0 ? 'no loss left for today'
      : ['max loss', 'loss left today'].includes(bound) ? 'one share risks ' + risk.toFixed(2) + ' plus charges, more than the ' + maxLoss.toFixed(2) + ' allowed'
        : 'one share is more than your ' + bound;
    return out;
  }
  out.loss_at_stop = r2(qty * risk + trip(side, qty, entry, stop));
  out.targets = targets.map(t => { const gross = r2(qty * (t - entry) * k); return { price: t, gross, net: r2(gross - trip(side, qty, entry, t)) }; });
  return out;
}

// ---- paper trades: bars are [utc_ts, o, h, l, c, v]; times in the trade are IST 'YYYY-MM-DD HH:MM'
const istOf = (txt) => Date.parse(txt.replace(' ', 'T') + ':00Z') / 1000 - IST;
const hhmmOf = (ts) => new Date((ts + IST) * 1000).toISOString().slice(11, 16);
const dayOf = (ts) => new Date((ts + IST) * 1000).toISOString().slice(0, 10);

export function replay(t, bars, now, s) {
  const side = t.side, k = side === 'buy' ? 1 : -1, placed = istOf(t.placed), day = t.placed.slice(0, 10);
  const done = bars.filter(b => b[0] + BAR <= now && dayOf(b[0]) === day), after = done.filter(b => b[0] >= placed);
  const r = { id: t.id, symbol: t.symbol, side, qty: t.qty, stop: t.stop, target: t.target, target_name: t.target_name || '',
    placed: t.placed.slice(11), mode: t.mode, fill: null, exit: null, status: 'waiting', pnl: 0, charges: 0 };
  let i0 = null;
  const manual = t.exit, manualAt = manual ? istOf(day + ' ' + manual.time) : null;
  if (t.mode === 'market') { r.fill = { price: t.entry, time: t.placed.slice(11) }; i0 = 0; }
  else {
    const cutoff = s.no_entries_after;
    for (let i = 0; i < after.length; i++) {
      const b = after[i];
      if (hhmmOf(b[0] + BAR) > cutoff || (manualAt !== null && b[0] >= manualAt)) break;
      if (k > 0 ? b[4] > t.entry : b[4] < t.entry) { r.fill = { price: b[4], time: hhmmOf(b[0] + BAR) }; i0 = i + 1; break; }
    }
    if (!r.fill) {
      if (manual) { r.status = 'cancelled'; return r; }
      const lastEnd = done.length ? hhmmOf(done[done.length - 1][0] + BAR) : '09:15';
      if (lastEnd >= cutoff || hhmmOf(now) >= cutoff) r.status = 'cancelled';
      return r;
    }
  }
  const fill = r.fill.price, flat = s.flat_by;
  for (const b of after.slice(i0)) {
    if (manualAt !== null && b[0] >= manualAt) break;
    if (hhmmOf(b[0]) >= flat) { r.exit = { how: 'flat', price: b[1], time: hhmmOf(b[0]) }; break; }
    const end = hhmmOf(b[0] + BAR);
    if (k > 0 ? b[3] <= t.stop : b[2] >= t.stop) { r.exit = { how: 'stop', price: k > 0 ? Math.min(t.stop, b[1]) : Math.max(t.stop, b[1]), time: end }; break; }
    if (k > 0 ? b[2] >= t.target : b[3] <= t.target) { r.exit = { how: 'target', price: k > 0 ? Math.max(t.target, b[1]) : Math.min(t.target, b[1]), time: end }; break; }
  }
  if (!r.exit && manual) r.exit = { how: 'manual', price: manual.price, time: manual.time };
  if (!r.exit && done.length && hhmmOf(done[done.length - 1][0]) >= flat) {
    const b = done[done.length - 1]; r.exit = { how: 'flat', price: b[4], time: hhmmOf(b[0] + BAR) };
  }
  const out = r.exit ? r.exit.price : (done.length ? done[done.length - 1][4] : fill);
  r.charges = r2(trip(side, t.qty, fill, out));
  r.pnl = r2(t.qty * (out - fill) * k - r.charges);
  r.status = r.exit ? 'closed' : 'open';
  return r;
}

export function dayBook(trades, barsBySym, now, s) {
  const today = dayOf(now);
  const rows = trades.filter(t => t.placed.slice(0, 10) === today).map(t => replay(t, barsBySym[t.symbol] || [], now, s));
  const sum = (st) => r2(rows.filter(r => r.status === st).reduce((a, r) => a + r.pnl, 0));
  const realized = sum('closed'), open = sum('open'), taken = rows.filter(r => r.status !== 'cancelled').length;
  const todays = trades.filter(t => t.placed.slice(0, 10) === today);
  const inUse = r2(rows.reduce((a, r, i) => a + (['open', 'waiting'].includes(r.status) ? r.qty * (r.fill ? r.fill.price : todays[i].entry) : 0), 0));
  let lock = null;
  const left = Number(s.max_daily_loss_inr) + Math.min(realized, 0);
  if (left < 0.25 * Number(s.max_loss_per_trade_inr)) lock = 'Daily loss limit reached (' + (-realized).toFixed(2) + ' of ' + Number(s.max_daily_loss_inr).toFixed(2) + ' lost). Stop for today.';
  else if (taken >= Number(s.max_trades_per_day)) lock = 'Trade limit reached (' + taken + ' of ' + Number(s.max_trades_per_day) + '). Stop for today.';
  else if (hhmmOf(now) >= s.no_entries_after) lock = 'No new trades after ' + s.no_entries_after + '.';
  return { trades: rows, realized, open, net: r2(realized + open), taken, in_use: inUse, max_trades: Number(s.max_trades_per_day),
    max_daily_loss: Number(s.max_daily_loss_inr), lock };
}

// ---- the mood
export function resolvedToday(boardRows, now) {
  let t1 = 0, stop = 0;
  const clock = hhmmOf(now);
  for (const row of boardRows) {
    const p = row.plan || {};
    for (const side of ['buy', 'sell']) {
      const x = p[side] || {};
      if (x.status !== 'triggered') continue;
      if (x.hits && x.hits.length && x.hits[0] <= clock) t1++;
      else if (!(x.hits && x.hits.length) && x.exit && x.exit.how === 'stop' && x.exit.time <= clock) stop++;
    }
  }
  return [t1, stop];
}

export function mood(boardRows, now, book, marketOpen = true) {
  const [t1, stop] = resolvedToday(boardRows, now), n = t1 + stop, clock = hhmmOf(now);
  if (!marketOpen) return { level: 'closed', label: 'Market closed', factor: 1, t1, stop, reasons: ['NSE is closed: the desk opens at 9:15 AM on a trading day.'] };
  if (book.lock) return { level: 'sit', label: 'Sit out', factor: 0, t1, stop, reasons: [book.lock] };
  const reasons = [];
  let level = 'trade', factor = 1;
  if (clock < '09:30') { level = 'wait'; reasons.push('The opening range (9:15 to 9:30) is still forming: levels at 9:30 AM.'); }
  if (n >= CHOP_MIN && stop / n >= CHOP_SHARE) {
    level = 'small'; factor = 0.5;
    reasons.push('Choppy: ' + stop + ' of ' + n + ' breakouts today hit their stop before T1. On 59 past sessions, breakouts after a day turned like this made +0.06R before charges against +0.13R: half size.');
  } else if (n) reasons.push(t1 + ' of ' + n + " of today's breakouts reached T1 before the stop.");
  if (clock >= '09:30' && clock <= '10:00') reasons.push('9:35 to 10:00 has been the best window: T1 49 % of the time, +0.06R after charges.');
  else if (clock > '10:00' && level === 'trade') reasons.push('After 10:00, breakouts reached T1 less often (39 % falling to 22 %): be picky.');
  return { level, label: { trade: 'Trade', small: 'Trade small', wait: 'Wait for 9:30' }[level], factor, t1, stop, reasons };
}

// ---- a new paper trade, checked (new_trade in chart_desk.py). last: the last closed bar [utc_ts, o, h, l, c, v].
export class DeskError extends Error {}
export function newTrade(sym, plan, side, targetN, last, nowText, s, book, md, id) {
  if (side !== 'buy' && side !== 'sell') throw new DeskError('side must be buy or sell');
  if (side === 'sell' && !s.allow_short) throw new DeskError("Short selling is off: turn on 'Allow SELL (short)' in the desk settings first. A short is intraday only (MIS): it must be bought back the same day.");
  if (md.level === 'closed') throw new DeskError(md.reasons[0]);     // a closed day says so before any limit does
  if (book.lock) throw new DeskError(book.lock);
  if (['closed', 'wait', 'sit'].includes(md.level)) throw new DeskError(md.reasons && md.reasons.length ? md.reasons[0] : 'Not now.');
  if (!plan || plan.status !== 'ready') throw new DeskError('No levels for ' + sym + ' today yet.');
  if ((book.trades || []).some(r => r.symbol === sym && ['open', 'waiting'].includes(r.status))) throw new DeskError('You already have a paper trade on ' + sym + ': exit or cancel it first.');
  const x = plan[side];
  if (![1, 2, 3].includes(targetN)) throw new DeskError('Pick T1, T2 or T3.');
  const target = x.targets[targetN - 1], k = side === 'buy' ? 1 : -1;
  if (x.status === 'none') throw new DeskError('The ' + side.toUpperCase() + ' side did not trigger by ' + s.no_entries_after + ': no entry today.');
  let entry, mode;
  if (x.status === 'triggered') {
    if (x.exit || (x.hits && x.hits.length >= targetN)) throw new DeskError('This ' + side.toUpperCase() + ' already ' + (x.exit ? 'ended (' + x.exit.how + ')' : 'reached T' + targetN) + ': too late to enter.');
    if (!last) throw new DeskError('No closed bar yet.');
    entry = last[4]; mode = 'market';
    if ((entry - target) * k >= 0) throw new DeskError('The price ' + entry.toFixed(2) + ' is already past T' + targetN + ' ' + target.toFixed(2) + '.');
    if ((entry - x.stop) * k <= 0) throw new DeskError('The price ' + entry.toFixed(2) + ' is already past the stop ' + x.stop.toFixed(2) + '.');
  } else { entry = x.entry; mode = 'armed'; }
  const sz = sizeTrade(side, entry, x.stop, x.targets, s, book.realized || 0, md.factor == null ? 1 : md.factor, book.in_use || 0);
  if (!sz.qty) throw new DeskError('No trade: ' + (sz.refusal || '0 shares') + '.');
  return { id, symbol: sym, side, qty: sz.qty, mode, entry, stop: x.stop, target, target_name: 'T' + targetN, placed: nowText, size: sz };
}
