// Checks the Worker's desk (src/desk.js) against nse-chart/chart_desk.py on the saved Nifty 50 bars (October 30
// 2026): sizing every BUY and SELL at full size, half size and after a loss; replaying armed, market and
// exited-by-hand paper trades; the day's book and lock; the mood at five times of the day.
//   node cloudflare/test-desk.mjs
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { parseChart, atIst } from './src/core.js';
import { settingsFrom, sizeTrade, replay, dayBook, mood } from './src/desk.js';

const FIXTURE = 'tests/fixtures/nse/nifty50-5m-60d.json.gz';
const TIMES = ['2026-10-30 09:25', '2026-10-30 09:50', '2026-10-30 10:40', '2026-10-30 13:00', '2026-10-30 15:40'];
const bundle = JSON.parse(gunzipSync(readFileSync(FIXTURE)).toString('utf8'));
const py = `
import sys, json, datetime; sys.path.insert(0, 'nse-chart'); import live, chart_desk as d
F = ${JSON.stringify(FIXTURE)}; s = d.settings_from()
out = {'size': {}, 'trades': [], 'replay': {}, 'book': {}, 'mood': {}, 'rows': {}}
clock = live.Clock('2026-10-30 15:40'); store = live.Store(live.Source(clock, F), clock); store.refresh_board()
bars = {sym: store.source.chart(sym, '60d')[0] for sym in store.today}
tid = 0
for sym in sorted(store.today):
    p = live.orb_plan(sym, store.today[sym], clock.now())
    if p.get('status') != 'ready':
        continue
    for side in ('buy', 'sell'):
        x = p[side]
        for f, real in ((1, 0), (0.5, 0), (1, -1200), (1, -1500)):
            out['size']['%s|%s|%s|%s' % (sym, side, f, real)] = d.size_trade(side, x['entry'], x['stop'], x['targets'], s, real, f)
        tid += 1
        n = tid % 3
        t = {'id': tid, 'symbol': sym, 'side': side, 'qty': 10 + tid, 'mode': 'armed', 'entry': x['entry'], 'stop': x['stop'],
             'target': x['targets'][n], 'target_name': 'T%d' % (n + 1), 'placed': '2026-10-30 09:3%d' % (tid % 10)}
        if tid % 4 == 0:
            last = [b for b in bars[sym] if b[0].date() == datetime.date(2026, 10, 30) and b[0] + live.BAR <= datetime.datetime(2026, 10, 30, 10, 30)][-1]
            t.update(mode='market', entry=last[4], placed='2026-10-30 10:30')
        if tid % 5 == 0:
            t['exit'] = {'price': x['entry'], 'time': '11:00'}
        out['trades'].append(t)
for now in ${JSON.stringify(TIMES)}:
    c = live.Clock(now); st = live.Store(live.Source(c, F), c); st.refresh_board()
    for t in out['trades']:
        out['replay'][now + '|' + str(t['id'])] = d.replay(t, bars[t['symbol']], c.now(), s)
    book = d.day_book(out['trades'][:6], bars, c.now(), s)
    out['book'][now] = book
    out['rows'][now] = st.board['stocks']
    out['mood'][now] = d.mood(st.board['stocks'], c.now(), book)
print(json.dumps(out))
`;
const want = JSON.parse(execFileSync('python', ['-c', py], { maxBuffer: 1 << 28 }).toString('utf8'));
const s = settingsFrom({});
const bars = {};
for (const sym of Object.keys(bundle)) bars[sym] = parseChart(bundle[sym])[0];
let n = 0, bad = 0;
const same = (key, got, exp) => { n++; const a = JSON.stringify(got), b = JSON.stringify(exp); if (a !== b) { bad++; if (bad <= 6) console.log('DIFF', key, '\n  js', a, '\n  py', b); } };
const planOf = (k) => k.split('|');
for (const [key, exp] of Object.entries(want.size)) {
  const [, side, f, real] = planOf(key);
  const targets = exp.targets ? exp.targets.map(x => x.price) : [];
  same('size ' + key, sizeTrade(side, exp.entry, exp.stop, targets, s, Number(real), Number(f)), exp);
}
for (const now of TIMES) {
  const ts = atIst(now.slice(0, 10), +now.slice(11, 13) * 60 + +now.slice(14, 16));
  const cut = {}; for (const k of Object.keys(bars)) cut[k] = bars[k].filter(b => b[0] < ts);
  for (const t of want.trades) same('replay ' + now + ' #' + t.id, replay(t, cut[t.symbol], ts, s), want.replay[now + '|' + t.id]);
  const book = dayBook(want.trades.slice(0, 6), cut, ts, s);
  same('book ' + now, book, want.book[now]);
  same('mood ' + now, mood(want.rows[now], ts, book), want.mood[now]);
}
console.log(bad ? bad + ' of ' + n + ' desk checks differ' : 'all ' + n + ' desk checks match chart_desk.py');
process.exit(bad ? 1 : 0);
