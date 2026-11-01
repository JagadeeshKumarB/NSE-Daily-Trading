// Checks the levels the chart page draws (dayLevels in nse-chart/page.html) against orb_plan in live.py, which
// feeds the watchlist, the phone alerts and the Worker: every symbol of the saved Nifty 50 bars, five times a day.
//   node cloudflare/test-page-levels.mjs
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const page = readFileSync('nse-chart/page.html', 'utf8').replace(/\r\n/g, '\n');
const grab = (re) => { const m = page.match(re); if (!m) throw new Error('not in page.html: ' + re); return m[0]; };
const src = [
  grab(/const IST = 19800[^\n]*\n/),
  grab(/function tickSize\(p, isIndex\) \{[\s\S]*?\n\}\n/),
  grab(/function toTick\(p, mode, t\) \{[\s\S]*?\n\}\n/),
  grab(/const mins = [^\n]*\n/),
  grab(/const OPEN = [^\n]*\n/),
  grab(/function rangeLevels\([\s\S]*?\n\}\nfunction dayLevels\([\s\S]*?\n\}\n/),
].join('');
const { dayLevels } = new Function(src + 'return { dayLevels };')();

const TIMES = ['2026-10-30 09:32', '2026-10-30 09:50', '2026-10-30 11:30', '2026-10-30 14:40', '2026-10-30 16:00'];
const py = `
import sys, json; sys.path.insert(0, 'nse-chart'); import live
out = {}
for now in ${JSON.stringify(TIMES)}:
    clock = live.Clock(now); store = live.Store(live.Source(clock, 'tests/fixtures/nse/nifty50-5m-60d.json.gz'), clock)
    store.refresh_board()
    for sym, today in store.today.items():
        bars = [[live.utc_ts(b[0])] + b[1:] for b in today if b[0] + live.BAR <= clock.now()]
        out[now + '|' + sym] = {'bars': bars, 'plan': live.orb_plan(sym, today, clock.now())}
print(json.dumps(out))
`;
const want = JSON.parse(execFileSync('python', ['-c', py], { maxBuffer: 1 << 28 }).toString('utf8'));
const hhmm = (m) => String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');

let n = 0, bad = 0;
for (const [key, { bars, plan }] of Object.entries(want)) {
  n++;
  const got = bars.length ? dayLevels(bars, false) : { status: 'forming' };
  const pyS = plan.status, jsS = got.status === 'ready' ? 'ready' : got.status;
  const diffs = [];
  if (pyS !== jsS) diffs.push('status ' + pyS + ' vs ' + jsS);
  else if (pyS === 'ready') {
    for (const side of ['buy', 'sell']) {
      const p = plan[side], j = got[side];
      const jStatus = j.status === 'trig' ? 'triggered' : j.status;
      const same = (a, b, what) => { if (JSON.stringify(a) !== JSON.stringify(b)) diffs.push(side + ' ' + what + ' ' + JSON.stringify(a) + ' vs ' + JSON.stringify(b)); };
      same(p.entry, j.entry, 'entry'); same(p.stop, j.stop, 'stop'); same(p.targets, j.targets, 'targets'); same(p.status, jStatus, 'status');
      if (p.status === 'triggered') {
        same(p.entry_time, hhmm(j.etime), 'entry time');
        same(p.hits, j.hits.map(h => hhmm(h.m)), 'targets hit');
        const px = p.exit, jx = j.exit;
        same(px ? [px.how, px.price, px.time] : null, jx ? [jx.how, jx.px, hhmm(jx.m)] : null, 'exit');
      }
    }
  }
  if (diffs.length) { bad++; if (bad <= 8) console.log('DIFF', key, diffs.join('; ')); }
}
console.log(bad ? bad + ' of ' + n + ' differ' : 'all ' + n + ' chart levels match live.py');
process.exit(bad ? 1 : 0);
