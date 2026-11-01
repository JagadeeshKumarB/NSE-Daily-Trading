// Checks the Worker's maths against nse-chart/live.py on the saved Nifty 50 bars (October 30 2026):
// the bias read, the 60-session record and the default trade plan, for every symbol at three times.
//   node cloudflare/test-parity.mjs
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { parseChart, payload, closedBars, backtest, describeBacktest, orbPlan, volumeSurge, dateOf, atIst } from './src/core.js';

const FIXTURE = 'tests/fixtures/nse/nifty50-5m-60d.json.gz';
const TIMES = ['2026-10-30 09:50', '2026-10-30 12:00', '2026-10-30 16:00'];
const bundle = JSON.parse(gunzipSync(readFileSync(FIXTURE)).toString('utf8'));
const syms = Object.keys(bundle).sort();

const py = `
import sys, json; sys.path.insert(0, 'nse-chart'); import live
out = {}
for now in ${JSON.stringify(TIMES)}:
    clock = live.Clock(now); store = live.Store(live.Source(clock, ${JSON.stringify(FIXTURE)}), clock)
    for sym in ${JSON.stringify(syms)}:
        p = store.chart(sym); bars, _ = store.source.chart(sym, '60d')
        bt = live.backtest(live.closed_bars(bars, clock.now())); bt['lines'] = live.describe_backtest(bt)
        today = store.today[sym]
        out[now + '|' + sym] = {'bias': p['bias'], 'record': bt, 'plan': live.orb_plan(sym, today, clock.now()), 'surge': live.volume_surge(bars, clock.now())}
print(json.dumps(out))
`;
const want = JSON.parse(execFileSync('python', ['-c', py], { maxBuffer: 1 << 28 }).toString('utf8'));

let bad = 0, n = 0;
for (const t of TIMES) {
  const now = atIst(t.slice(0, 10), +t.slice(11, 13) * 60 + +t.slice(14, 16));
  for (const sym of syms) {
    const [all, meta] = parseChart(bundle[sym]);
    const b = all.filter(x => x[0] < now);
    const p = payload(sym, b, meta, now);
    const bt = backtest(closedBars(b, now)); bt.lines = describeBacktest(bt);
    const got = { bias: p.bias, record: bt, plan: orbPlan(sym, p.bars.filter(x => dateOf(x[0]) === p.session), now), surge: volumeSurge(b, now) };
    for (const k of ['bias', 'record', 'plan', 'surge']) {
      n++;
      const a = JSON.stringify(got[k]), w = JSON.stringify(want[t + '|' + sym][k]);
      if (a !== w) { bad++; if (bad <= 5) console.log('DIFF', t, sym, k, '\n  js', a, '\n  py', w); }
    }
  }
}
console.log(bad ? `${bad} of ${n} checks differ` : `all ${n} checks match live.py`);
process.exit(bad ? 1 : 0);
