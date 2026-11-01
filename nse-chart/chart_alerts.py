"""Live Chart phone alerts, free: email (SMTP), WhatsApp (CallMeBot) and ntfy. The morning levels,
each trigger, target, stop and 15:10 exit, and the day's summary. Every message says nothing was placed."""

import json
import os
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

import datetime
from chart_data import *  # noqa: F401,F403
from chart_signals import *  # noqa: F401,F403

ALERT_ENV = {
    "email": ("NSE_SMTP_USER", "NSE_SMTP_PASSWORD", "NSE_ALERT_EMAIL_TO"),
    "whatsapp": ("NSE_WHATSAPP_PHONE", "NSE_WHATSAPP_APIKEY"),
    "ntfy": ("NSE_NTFY_TOPIC",),
}
CHUNK = 1500         # characters per WhatsApp or ntfy message


def chunks(text, size=CHUNK):
    out, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


class Notifier:
    """Sends to every channel whose environment variables are set. Secrets stay in the environment:
    they are never printed, logged or written to a file."""

    def __init__(self, env=None):
        self.env = os.environ if env is None else env
        self.channels = [c for c, keys in ALERT_ENV.items() if all(self.env.get(k) for k in keys)]

    def send(self, subject, text):
        results = []
        for c in self.channels:
            try:
                getattr(self, "_" + c)(subject, text)
                results.append((c, True, ""))
            except Exception as e:   # one channel failing must not stop the others
                results.append((c, False, "%s: %s" % (type(e).__name__, str(e).split("?")[0][:120])))
        return results

    def _email(self, subject, text):
        e = self.env
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, e["NSE_SMTP_USER"], e["NSE_ALERT_EMAIL_TO"]
        msg.set_content(text)
        host, port = e.get("NSE_SMTP_HOST", "smtp.gmail.com"), int(e.get("NSE_SMTP_PORT", "465"))
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(e["NSE_SMTP_USER"], e["NSE_SMTP_PASSWORD"])
            s.send_message(msg)

    def _whatsapp(self, subject, text):
        for i, part in enumerate(chunks(subject + "\n" + text)):
            if i:
                time.sleep(3)        # CallMeBot is free and slow: space the parts out
            q = urllib.parse.urlencode({"phone": self.env["NSE_WHATSAPP_PHONE"], "text": part,
                                        "apikey": self.env["NSE_WHATSAPP_APIKEY"]})
            with urllib.request.urlopen("https://api.callmebot.com/whatsapp.php?" + q, timeout=30) as r:
                body = r.read().decode("utf-8", "replace").lower()
            if "error" in body or "not" in body and "sent" not in body:
                raise ChartError("CallMeBot did not send it: check the phone number and API key")

    def _ntfy(self, subject, text):
        for part in chunks(text):
            req = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(self.env["NSE_NTFY_TOPIC"]),
                                         data=part.encode("utf-8"), headers={"Title": subject})
            urllib.request.urlopen(req, timeout=30).close()


def money(x):
    return inr(x)[1:]              # 1,394.50: the rupee sign is noise in a list of 50


def side_levels(s):
    return "%s SL %s T1 %s T2 %s T3 %s" % (money(s["entry"]), money(s["stop"]), *(money(t) for t in s["targets"]))


def level_lines(plans):
    """Two lines a symbol (BUY, SELL): the index first, then the stocks, alphabetical."""
    lines = []
    for sym in sorted(plans, key=lambda s: (s != INDEX, s)):
        p = plans[sym]
        if p["status"] == "forming":
            lines.append("%-11s range still forming" % sym)
        elif p["status"] == "refused":
            lines.append("%-11s no levels: %s" % (sym, p["why"]))
        else:
            for side, word in (("buy", "BUY  >"), ("sell", "SELL <")):
                s = p[side]
                tag = "  (IN %s)" % s["entry_time"] if s["status"] == "triggered" else ""
                lines.append("%-11s %s %s%s" % (sym if side == "buy" else "", word, side_levels(s), tag))
    return lines


def morning_text(day, plans):
    return "\n".join([
        "NSE opening-range levels, %s (IST). Paper first. Not financial advice." % day.strftime("%a %d %b %Y"),
        "BUY only if a 5-minute bar CLOSES above the range high, SELL (intraday short) only if one closes below the low, by 2:30 PM.",
        "SL = the other side of the range. T1, T2, T3 = 1, 2, 3 range widths. Out by 15:10.",
        "IN = already triggered (time).",
        ""] + level_lines(plans))


def side_events(sym, side, s, sent):
    """New events for one side, in order: the trigger, each target, the exit. Marks them in `sent`."""
    word, out = side.upper(), []
    if s.get("status") != "triggered":
        return out
    key = "%s:%s" % (side, sym)
    if key not in sent:
        sent.add(key)
        out.append("%s %s %s: %s %s. Nothing placed." % (word, sym, s["entry_time"],
                                                         "above" if side == "buy" else "below", side_levels(s)))
    for n, t in enumerate(s["hits"], 1):
        if "%s:T%d" % (key, n) not in sent:
            sent.add("%s:T%d" % (key, n))
            out.append("T%d hit %s %s %s at %s." % (n, word, sym, t, money(s["targets"][n - 1])))
    x = s["exit"]
    if x and x["how"] != "t3" and key + ":exit" not in sent:
        sent.add(key + ":exit")
        out.append("%s %s %s %s at %s (entry %s, %d of 3 targets)." % (
            "SL hit" if x["how"] == "stop" else "OUT at 15:10", word, sym, x["time"], money(x["price"]),
            money(s["entry"]), len(s["hits"])))
    return out


def summary_text(day, plans):
    lines, counts = [], []
    for side in ("buy", "sell"):
        trig = [p[side] for p in plans.values() if p.get(side, {}).get("status") == "triggered"]
        n = lambda f: sum(1 for s in trig if f(s))  # noqa: E731
        counts.append("%s %d: T1 %d, T2 %d, T3 %d, SL before T1 %d" % (
            side.upper(), len(trig), n(lambda s: len(s["hits"]) >= 1), n(lambda s: len(s["hits"]) >= 2),
            n(lambda s: len(s["hits"]) == 3), n(lambda s: not s["hits"] and (s["exit"] or {}).get("how") == "stop")))
    lines.append("NSE day summary, %s, %d symbols. %s." % (day.strftime("%a %d %b %Y"), len(plans), "; ".join(counts)))
    lines += ["Before charges, on the opening-range levels. One day is one data point.", ""]
    for sym in sorted(plans, key=lambda s: (s != INDEX, s)):
        for side in ("buy", "sell"):
            s = plans[sym].get(side)
            if not s or s["status"] != "triggered":
                continue
            x = s["exit"] or {"how": "open", "price": s["entry"], "time": "-"}
            how = {"t3": "T3", "stop": "SL", "flat": "out 15:10", "open": "open"}[x["how"]]
            lines.append("%-11s %-4s in %s  %s  hit %d/3 -> %s %s" % (sym, side.upper(), s["entry_time"], side_levels(s),
                                                                     len(s["hits"]), how, money(x["price"])))
    return "\n".join(lines)


class Alerter:
    """After each list refresh: the morning levels once, each new breakout and exit, the summary after 15:30.
    Remembers what it sent in trader/alerts-state.json, so a restart never sends the same alert twice."""

    def __init__(self, store, notifier, folder="trader", echo=print, mode="all"):
        self.store, self.notifier, self.folder, self.echo = store, notifier, folder, echo
        self.mode = mode           # all: levels, each breakout and exit, summary; daily: levels and summary

    def _state(self, day):
        try:
            with open(os.path.join(self.folder, "alerts-state.json"), encoding="utf-8") as fh:
                s = json.load(fh)
            if s.get("date") == day.isoformat():
                return s
        except (OSError, ValueError):
            pass
        return {"date": day.isoformat(), "sent": []}

    def _save(self, state, subject, text, results):
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, "alerts-state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        stamp = self.store.clock.now().strftime("%H:%M:%S")
        sent = ", ".join("%s %s" % (c, "ok" if ok else "FAILED (%s)" % err) for c, ok, err in results) or "no phone channel set"
        with open(os.path.join(self.folder, "alerts-%s.txt" % state["date"]), "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n%s\n-> %s\n\n" % (stamp, subject, text, sent))
        self.echo("[%s] %s -> %s" % (stamp, subject, sent))

    def plans(self):
        now = self.store.clock.now()
        with self.store.lock:
            today = dict(self.store.today)
        return {s: orb_plan(s, bars, now) for s, bars in today.items() if bars and bars[0][0].date() == now.date()}

    def tick(self):
        now = self.store.clock.now()
        if not trading_day(now.date()) or now.time() < OR_END:
            return None
        try:
            self.store.chart(INDEX)
        except ChartError:
            pass
        plans = self.plans()
        if not plans:
            return None
        state = self._state(now.date())
        sent = set(state["sent"])
        parts, subject = [], None
        if now.time() >= CLOSE:
            if "summary" not in sent:
                subject, parts = "NSE summary %s" % now.strftime("%d %b"), [summary_text(now.date(), plans)]
                sent.add("summary")
        else:
            ready = sum(1 for p in plans.values() if p["status"] != "forming")
            if "morning" not in sent and (ready >= 0.8 * len(plans) or now.time() >= datetime.time(9, 40)):
                subject, parts = "NSE levels %s" % now.strftime("%d %b"), [morning_text(now.date(), plans)]
                sent.add("morning")
                sent.update("%s:%s" % (side, s) for s, p in plans.items() for side in ("buy", "sell")
                            if p.get(side, {}).get("status") == "triggered")   # the list already says IN
            events = []
            for s in sorted(plans, key=lambda s: (s != INDEX, s)) if self.mode == "all" else []:
                for side in ("buy", "sell"):
                    if side in plans[s]:
                        events += side_events(s, side, plans[s][side], sent)
            if events:
                parts.append("\n".join(events))
                subject = subject or "NSE alert %s: %d new" % (now.strftime("%H:%M"), len(events))
        if not parts:
            return None
        text = "\n\n".join(parts)
        results = self.notifier.send(subject, text)
        state["sent"] = sorted(sent)
        self._save(state, subject, text, results)
        return subject, text, results
