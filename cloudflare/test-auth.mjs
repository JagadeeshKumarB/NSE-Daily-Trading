// Checks the sign-in (src/auth.js) with a pretend D1 and a pretend email service: codes, tries, limits,
// sessions and sign-out.   node cloudflare/test-auth.mjs
import assert from 'node:assert/strict';
import { signedIn, authRoute } from './src/auth.js';

const rows = new Map();
const DB = { prepare: (sql) => ({ bind: (...a) => ({
  first: async () => { const r = rows.get(a[0]); return r ? (sql.includes('SELECT t') ? { t: r.t } : { v: r.v }) : null; },
  run: async () => { rows.set(a[0], { t: a[1], v: a[2] }); },
}) }) };
const env = { DB, SESSION_SECRET: 'x'.repeat(48), ALLOWED_EMAILS: 'me@gmail.com', RESEND_API_KEY: 'test' };
const mails = [];
globalThis.fetch = async (url, init) => { mails.push(JSON.parse(init.body)); return new Response('{}', { status: 200 }); };

const ORIGIN = 'https://chart.example';
const post = (path, data, extra = {}) => authRoute(new Request(ORIGIN + path, { method: 'POST',
  headers: { 'Content-Type': 'application/json', Origin: ORIGIN, ...extra }, body: JSON.stringify(data) }), env, path);
const lastCode = () => mails[mails.length - 1].subject.match(/(\d{6})$/)[1];
const cookieOf = (r) => r.headers.get('Set-Cookie').split(';')[0];
const page = (cookie) => new Request(ORIGIN + '/', { headers: cookie ? { Cookie: cookie } : {} });

// an uninvited email gets the same answer as an invited one, and no email
let r = await post('/auth/start', { email: 'stranger@gmail.com' });
assert.equal(r.status, 200);
assert.equal(mails.length, 0);
assert.equal((await post('/auth/verify', { email: 'stranger@gmail.com', code: '123456' })).status, 401);

// the invited email gets a code; a second request within a minute is refused
r = await post('/auth/start', { email: ' Me@Gmail.com ' });
assert.equal(r.status, 200);
assert.equal(mails.length, 1);
assert.deepEqual(mails[0].to, ['me@gmail.com']);
assert.equal((await post('/auth/start', { email: 'me@gmail.com' })).status, 429);
assert.ok(!JSON.stringify([...rows.values()]).includes(lastCode()), 'the code is stored hashed');

// wrong codes count down; the right code gives a session; the code then works no more
const good = lastCode(), bad = good === '000000' ? '111111' : '000000';
r = await post('/auth/verify', { email: 'me@gmail.com', code: bad });
assert.equal(r.status, 401);
assert.match((await r.json()).error, /4 tries left/);
r = await post('/auth/verify', { email: 'me@gmail.com', code: good });
assert.equal(r.status, 200);
const cookie = cookieOf(r);
assert.match(r.headers.get('Set-Cookie'), /HttpOnly; Secure; SameSite=Lax/);
assert.equal(await signedIn(page(cookie), env), 'me@gmail.com');
assert.equal((await post('/auth/verify', { email: 'me@gmail.com', code: good })).status, 401);

// a forged or altered cookie, no cookie, another secret, or a removed email: no way in
assert.equal(await signedIn(page(), env), '');
assert.equal(await signedIn(page(cookie.slice(0, -2) + 'AA'), env), '');
const [b, sig] = cookie.split('=')[1].split('.');
const forged = Buffer.from(JSON.stringify({ e: 'me@gmail.com', i: 9e9, x: 9e9 })).toString('base64url');
assert.equal(await signedIn(page('nse_session=' + forged + '.' + sig), env), '');
assert.equal(await signedIn(page(cookie), { ...env, SESSION_SECRET: 'y'.repeat(48) }), '');
assert.equal(await signedIn(page(cookie), { ...env, ALLOWED_EMAILS: 'other@gmail.com' }), '');
assert.equal(await signedIn(page(cookie), { ...env, SESSION_SECRET: '' }), '');

// a post from another site, or not JSON, is refused
assert.equal((await post('/auth/start', { email: 'me@gmail.com' }, { Origin: 'https://evil.example' })).status, 400);
assert.equal((await post('/auth/start', { email: 'me@gmail.com' }, { 'Content-Type': 'text/plain' })).status, 400);

// five wrong tries kill the code even if the sixth is right
rows.get('otp:me@gmail.com').t = 0;
const o = JSON.parse(rows.get('otp:me@gmail.com').v); o.sent = 0; rows.set('otp:me@gmail.com', { t: 0, v: JSON.stringify(o) });
await post('/auth/start', { email: 'me@gmail.com' });
const c2 = lastCode(), w2 = c2 === '000000' ? '111111' : '000000';
for (let k = 0; k < 5; k++) await post('/auth/verify', { email: 'me@gmail.com', code: w2 });
assert.equal((await post('/auth/verify', { email: 'me@gmail.com', code: c2 })).status, 401);

// sign out ends the session (on every device: it was issued before the sign-out)
await new Promise(res => setTimeout(res, 1100));
r = await authRoute(new Request(ORIGIN + '/auth/logout', { headers: { Cookie: cookie } }), env, '/auth/logout');
assert.equal(r.status, 302);
assert.match(r.headers.get('Set-Cookie'), /Max-Age=0/);
assert.equal(await signedIn(page(cookie), env), '');

console.log('all sign-in checks pass');
