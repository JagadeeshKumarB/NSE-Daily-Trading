// The lock: our own sign-in with a one-time email code, in place of a login service in front of the Worker.
//
//   POST /auth/start   {email}          an invited email gets a 6-digit code by email (Resend)
//   POST /auth/verify  {email, code}    the right code sets a signed session cookie for 24 hours
//   GET  /auth/logout                   clears it
//
// Only the emails in ALLOWED_EMAILS (a Worker secret) can ever get a code; everyone else gets the same
// answer, so the page never says which emails are invited. A code works once, lasts 10 minutes and allows
// 5 tries; one code a minute and 5 an hour an email. Codes are stored hashed in D1, never in the clear.
// SESSION_SECRET (a Worker secret) signs the cookie; without it, or without ALLOWED_EMAILS, nobody gets in.

const CODE_TTL = 600, MAX_TRIES = 5, RESEND_AFTER = 60, MAX_PER_HOUR = 5, SESSION_TTL = 86400;
const COOKIE = 'nse_session';
const enc = new TextEncoder();
const nowTs = () => Math.floor(Date.now() / 1000);

const b64u = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const unb64u = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), c => c.charCodeAt(0));

async function hmac(secret, text) {
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return b64u(await crypto.subtle.sign('HMAC', key, enc.encode(text)));
}
// compare two strings in time that does not depend on where they differ
function same(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

export const allowedEmails = (env) => String(env.ALLOWED_EMAILS || '').toLowerCase().split(',').map(s => s.trim()).filter(Boolean);
const ready = (env) => !!env.SESSION_SECRET && env.SESSION_SECRET.length >= 32 && allowedEmails(env).length > 0;
const cleanEmail = (x) => String(x || '').trim().toLowerCase().slice(0, 200);

// ---------------------------------------------------------------- the session cookie

export async function signedIn(request, env) {
  if (!ready(env)) return '';
  const m = (request.headers.get('Cookie') || '').match(new RegExp('(?:^|;\\s*)' + COOKIE + '=([A-Za-z0-9_-]+)\\.([A-Za-z0-9_-]+)'));
  if (!m) return '';
  try {
    if (!same(await hmac(env.SESSION_SECRET, 'session:' + m[1]), m[2])) return '';
    const s = JSON.parse(new TextDecoder().decode(unb64u(m[1])));
    // a session dies at its expiry, when its email leaves the list, or when every session was signed out
    const since = await signedOutAt(env, s.e);
    return s.x > nowTs() && s.i > since && allowedEmails(env).includes(s.e) ? s.e : '';
  } catch (e) {
    return '';
  }
}
async function sessionCookie(env, email) {
  const body = b64u(enc.encode(JSON.stringify({ e: email, i: nowTs(), x: nowTs() + SESSION_TTL })));
  const sig = await hmac(env.SESSION_SECRET, 'session:' + body);
  return COOKIE + '=' + body + '.' + sig + '; Path=/; Max-Age=' + SESSION_TTL + '; HttpOnly; Secure; SameSite=Lax';
}
const clearCookie = COOKIE + '=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax';

// Sign out ends every session of that email, on every device: sessions issued before this time are refused.
async function signedOutAt(env, email) {
  const row = await env.DB.prepare('SELECT t FROM kv WHERE k = ?').bind('signout:' + email).first();
  return row ? row.t : 0;
}

// ---------------------------------------------------------------- codes

async function codeRow(env, email) {
  const row = await env.DB.prepare('SELECT v FROM kv WHERE k = ?').bind('otp:' + email).first();
  return row ? JSON.parse(row.v) : null;
}
const saveCode = (env, email, v) => env.DB.prepare('INSERT OR REPLACE INTO kv (k, t, v) VALUES (?, ?, ?)').bind('otp:' + email, nowTs(), JSON.stringify(v)).run();

function sixDigits() {
  const a = new Uint32Array(1);
  let n;
  do { crypto.getRandomValues(a); n = a[0]; } while (n >= 4294000000);   // no bias toward low codes
  return String(n % 1000000).padStart(6, '0');
}

async function sendCode(env, email, code) {
  if (!env.RESEND_API_KEY) throw new Error('email is not set up (RESEND_API_KEY)');
  const html = `<div style="font-family:Segoe UI,Arial,sans-serif;background:#0b0e14;padding:32px 16px">
<div style="max-width:440px;margin:0 auto;background:#161a25;border:1px solid #262b3a;border-radius:16px;padding:28px;color:#e6e8ee">
<div style="font-weight:700;font-size:16px">NSE Live <span style="color:#8b90a0;font-weight:400">· Private trading desk</span></div>
<p style="color:#c9cdd8;font-size:15px;line-height:1.5;margin:22px 0 14px">Hello,<br>Use this code to sign in to your NSE Live trading desk:</p>
<div style="font:700 34px/1 Consolas,monospace;letter-spacing:10px;color:#22ab94;padding:14px 0">${code}</div>
<p style="color:#c9cdd8;font-size:14px;line-height:1.5;margin:14px 0 0">The code works once and expires in 10 minutes. Do not share it with anyone.</p>
<p style="color:#8b90a0;font-size:13px;line-height:1.5;margin:14px 0 0">If you did not ask to sign in, you can ignore this email. No one can open your desk without this code.</p>
<p style="color:#5f6474;font-size:12px;margin:22px 0 0;border-top:1px solid #262b3a;padding-top:14px">NSE Live · nselive.pages.dev · Not financial advice.</p>
</div></div>`;
  const text = 'Hello,\n\nUse this code to sign in to your NSE Live trading desk: ' + code + '\n\n'
    + 'The code works once and expires in 10 minutes. Do not share it with anyone.\n'
    + 'If you did not ask to sign in, you can ignore this email. No one can open your desk without this code.\n\n'
    + 'NSE Live · nselive.pages.dev · Not financial advice.';
  const r = await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + env.RESEND_API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from: env.MAIL_FROM || 'NSE Live <onboarding@resend.dev>', to: [email],
      subject: 'Your NSE Live sign-in code: ' + code, html, text }),
  });
  if (!r.ok) throw new Error('the email service answered ' + r.status);
}

// ---------------------------------------------------------------- the routes

const reply = (code, body, cookie) => {
  const h = { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' };
  if (cookie) h['Set-Cookie'] = cookie;
  return new Response(JSON.stringify(body), { status: code, headers: h });
};

// a POST from this site only: JSON, and the browser's Origin is our own
async function body(request) {
  const origin = request.headers.get('Origin');
  if (origin && origin !== new URL(request.url).origin) return null;
  if (!(request.headers.get('Content-Type') || '').includes('application/json')) return null;
  try { return await request.json(); } catch (e) { return null; }
}

export async function authRoute(request, env, path) {
  if (path === '/auth/logout') {
    const who = await signedIn(request, env);
    if (who) await env.DB.prepare('INSERT OR REPLACE INTO kv (k, t, v) VALUES (?, ?, ?)').bind('signout:' + who, nowTs(), '"all"').run();
    return new Response(null, { status: 302, headers: { Location: '/?signedout=1', 'Set-Cookie': clearCookie, 'Cache-Control': 'no-store' } });
  }
  if (request.method !== 'POST') return reply(405, { error: 'POST only' });
  if (!ready(env)) return reply(503, { error: 'Sign-in is not set up yet.' });
  const b = await body(request);
  if (!b) return reply(400, { error: 'Bad request.' });
  const email = cleanEmail(b.email);
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return reply(400, { error: 'Enter a valid email.' });
  const invited = allowedEmails(env).includes(email);

  if (path === '/auth/start') {
    // the same answer for every email: the page never tells who is invited
    const sent = { ok: true, message: 'If this email is invited, a 6-digit code is on its way.' };
    if (!invited) return reply(200, sent);
    const now = nowTs(), old = await codeRow(env, email);
    const hour = (old && old.hour || []).filter(t => now - t < 3600);
    if (old && now - old.sent < RESEND_AFTER) return reply(429, { error: 'Wait a minute before asking for another code.', wait: RESEND_AFTER - (now - old.sent) });
    if (hour.length >= MAX_PER_HOUR) return reply(429, { error: 'Too many codes this hour. Try again later.' });
    const code = sixDigits();
    try {
      await sendCode(env, email, code);
    } catch (e) {
      console.log('sign-in email failed: ' + e.message);   // the reason goes to the Worker log, not the page
      return reply(502, { error: 'We could not send your code right now. Please try again in a few minutes.' });
    }
    await saveCode(env, email, { hash: await hmac(env.SESSION_SECRET, 'code:' + email + ':' + code), exp: now + CODE_TTL, tries: 0, sent: now, hour: hour.concat(now) });
    return reply(200, sent);
  }

  if (path === '/auth/verify') {
    const code = String(b.code || '').replace(/\D/g, '');
    const wrong = { error: 'That code is not right, or it expired. Ask for a new one.' };
    if (!invited || code.length !== 6) return reply(401, wrong);
    const row = await codeRow(env, email);
    if (!row || !row.hash || row.exp < nowTs() || row.tries >= MAX_TRIES) return reply(401, wrong);
    if (!same(await hmac(env.SESSION_SECRET, 'code:' + email + ':' + code), row.hash)) {
      row.tries += 1;
      await saveCode(env, email, row);
      return reply(401, row.tries >= MAX_TRIES ? { error: 'Too many wrong tries. Ask for a new code.' } : { error: 'That code is not right. ' + (MAX_TRIES - row.tries) + ' tries left.' });
    }
    await saveCode(env, email, { sent: row.sent, hour: row.hour });   // used: the code is gone
    return reply(200, { ok: true }, await sessionCookie(env, email));
  }
  return reply(404, { error: 'not found' });
}
