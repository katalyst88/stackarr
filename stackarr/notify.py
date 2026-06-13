"""Notifications. Apprise is the engine (100+ channels for almost no code);
on top of it Stackarr ships a polished email digest with three selectable
themes (light / dark / fun) and a live preview, plus a simple Discord
webhook. All optional and per-deployment; toggled from Settings."""
import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from . import config, db


def _email_due() -> bool:
    """Throttle digests to the configured frequency (immediate/daily/weekly)."""
    freq = db.get_meta("email_frequency", "immediate")
    if freq == "immediate":
        return True
    last = db.get_meta("last_email_ts")
    if not last:
        return True
    window = {"daily": 86400, "weekly": 604800}.get(freq, 0)
    return (time.time() - float(last)) >= window

log = logging.getLogger("stackarr.notify")

THEMES = {
    "light": dict(page="#f1f5f9", card="#ffffff", head="#1e293b", brand="#f0b27a",
                  tag="#94a3b8", title="#0f172a", sub="#64748b", reason="#475569",
                  foot="#f8fafc", footfg="#94a3b8", btnbg="#d98c3f", btnfg="#1a0f02",
                  headline="{n} suggestion{s} waiting for approval",
                  tagline="new arrivals on your shelf", btn="Review suggestions",
                  blurb="Curated from your listening history — every one comes with its reason."),
    "dark": dict(page="#0b1120", card="#1e293b", head="#0f172a", brand="#f0b27a",
                 tag="#64748b", title="#e2e8f0", sub="#94a3b8", reason="#94a3b8",
                 foot="#0f172a", footfg="#64748b", btnbg="#d98c3f", btnfg="#1a0f02",
                 headline="{n} suggestion{s} waiting for approval",
                 tagline="fresh from the stacks", btn="Review suggestions",
                 blurb="Picked from your listening history — every one has a reason, none of it AI."),
    "fun": dict(page="#fef3c7", card="#ffffff", head="#ea8a3f", brand="#7c2d12",
                tag="#9a3412", title="#1f2937", sub="#b45309", reason="#c2540a",
                foot="#fffbeb", footfg="#d97706", btnbg="#ea8a3f", btnfg="#3b1300",
                headline="📚 {n} fresh pick{s} for your ears!",
                tagline="hot off the stacks 🔥", btn="Show me the books →",
                blurb="Hand-picked from what you've been listening to. 🎧"),
}


def smtp_settings() -> dict:
    """Effective SMTP config: in-app DB settings override env defaults."""
    g = db.get_meta
    try:
        port = int(g("smtp_port", str(config.SMTP_PORT)) or config.SMTP_PORT)
    except ValueError:
        port = config.SMTP_PORT
    return {"host": g("smtp_host", config.SMTP_HOST), "port": port,
            "user": g("smtp_user", config.SMTP_USER), "pw": g("smtp_pass", config.SMTP_PASS),
            "from": g("smtp_from", config.SMTP_FROM or config.SMTP_USER),
            "to": g("smtp_to", config.SMTP_TO)}


def email_configured() -> bool:
    s = smtp_settings()
    return bool(s["host"] and s["to"])


def email_enabled() -> bool:
    return email_configured() and db.get_meta("email_enabled", "0") == "1"


def current_theme() -> str:
    t = db.get_meta("email_theme", "light")
    return t if t in THEMES else "light"


def _send_email(subject: str, text: str, html: str) -> bool:
    s = smtp_settings()
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, f"{config.APP_NAME} <{s['from']}>", s["to"]
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(s["host"], s["port"], timeout=30) as srv:
            srv.starttls()
            if s["user"]:
                srv.login(s["user"], s["pw"])
            srv.send_message(msg)
        return True
    except Exception as e:
        log.warning("email send failed: %s", e)
        return False


def _row(s: dict, t: dict) -> str:
    cover = (f'<img src="{s["cover"]}" width="56" height="56" style="border-radius:6px;'
             'object-fit:cover;display:block;">' if s.get("cover")
             else f'<div style="width:56px;height:56px;border-radius:6px;background:{t["tag"]};"></div>')
    return (f'<tr><td style="padding:10px 14px 10px 0;width:56px;vertical-align:top;">{cover}</td>'
            f'<td style="padding:10px 0;vertical-align:top;">'
            f'<div style="font-weight:600;color:{t["title"]};font-size:15px;">{s["title"]}</div>'
            f'<div style="color:{t["sub"]};font-size:13px;">{s["author"]}</div>'
            f'<div style="color:{t["reason"]};font-size:13px;font-style:italic;margin-top:3px;">{s["reason"]}</div>'
            f'</td></tr>')


def render_digest(pending: list[dict], theme: str | None = None, base_url: str = "") -> str:
    t = THEMES[theme if theme in THEMES else current_theme()]
    n = len(pending); plural = "s" if n != 1 else ""
    rows = "".join(_row(s, t) for s in pending)
    btn = (f'<a href="{base_url}/suggestions" style="display:inline-block;background:{t["btnbg"]};'
           f'color:{t["btnfg"]};font-weight:700;text-decoration:none;padding:11px 26px;'
           f'border-radius:8px;font-size:14px;">{t["btn"]}</a>') if base_url else ""
    return f"""<!doctype html><html><body style="margin:0;background:{t['page']};">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{t['page']};padding:28px 0;"><tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:{t['card']};border-radius:12px;overflow:hidden;max-width:94%;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
<tr><td style="background:{t['head']};padding:20px 28px;">
<span style="color:{t['brand']};font-size:19px;font-weight:700;">{config.APP_NAME}</span>
<span style="color:{t['tag']};font-size:13px;margin-left:10px;">{t['tagline']}</span></td></tr>
<tr><td style="padding:24px 28px 8px;">
<p style="margin:0 0 6px;color:{t['title']};font-size:16px;font-weight:600;">{t['headline'].format(n=n, s=plural)}</p>
<p style="margin:0;color:{t['sub']};font-size:13.5px;">{t['blurb']}</p></td></tr>
<tr><td style="padding:8px 28px 4px;"><table width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>
<tr><td style="padding:18px 28px 26px;" align="center">{btn}</td></tr>
<tr><td style="background:{t['foot']};padding:14px 28px;color:{t['footfg']};font-size:12px;">
You're getting this because email digests are on in {config.APP_NAME}. Change theme or turn off on the Settings page.
</td></tr></table></td></tr></table></body></html>"""


def _apprise(title: str, body: str):
    urls = list(config.APPRISE_URLS)
    if not urls:
        return
    try:
        import apprise
        ap = apprise.Apprise()
        for u in urls:
            ap.add(u)
        ap.notify(title=title, body=body)
    except Exception as e:
        log.warning("apprise notify failed: %s", e)


def _discord(text: str):
    hook = db.setting("discord_webhook", config.DISCORD_WEBHOOK)
    if not hook or db.get_meta("discord_enabled", "0") != "1":
        return
    try:
        requests.post(hook, json={"content": text}, timeout=10)
    except Exception as e:
        log.warning("discord webhook failed: %s", e)


def _custom_webhook(payload: dict):
    """POST a small JSON payload to a user-configured generic webhook —
    the 'other webhook options' channel alongside Discord/Apprise/email."""
    url = db.setting("custom_webhook", "")
    if not url:
        return
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.warning("custom webhook failed: %s", e)


def available(book: dict, base_url: str = ""):
    """Fire when a requested book lands in Audiobookshelf. Master switch is
    notify_avail_enabled; each channel still self-gates on its own config."""
    if db.get_meta("notify_avail_enabled", "0") != "1":
        return
    title, author = book.get("title", "a book"), book.get("author", "")
    base = base_url or db.get_meta("public_url", "")
    subject = f"{config.APP_NAME}: “{title}” is ready to listen"
    body = f"“{title}”{(' by ' + author) if author else ''} just landed in your Audiobookshelf library."
    _discord(f"📚 **{title}**{(' — ' + author) if author else ''} is now in your library."
             + (f" {base}" if base else ""))
    _apprise(subject, body)
    _custom_webhook({"event": "available", "title": title, "author": author, "url": base})
    if email_enabled():
        _send_email(subject, body, f"<p style='font-family:sans-serif;font-size:15px'>{body}</p>")


def suggestion_digest(pending: list[dict], base_url: str = "") -> bool:
    n = len(pending)
    text = f"{n} audiobook suggestion(s) waiting for approval:\n" + \
           "\n".join(f"  - {s['title']} — {s['author']}  ({s['reason']})" for s in pending)
    sent = False
    if email_enabled() and _email_due():
        sent = _send_email(f"{config.APP_NAME}: {n} suggestion{'s' if n!=1 else ''} awaiting approval",
                           text, render_digest(pending, base_url=base_url))
        if sent:
            db.set_meta("last_email_ts", str(time.time()))
    _apprise(f"{config.APP_NAME}: {n} suggestion(s) to review", text)
    _discord(f"**{config.APP_NAME}** — {n} suggestion(s) waiting for approval"
             + (f": {base_url}/suggestions" if base_url else ""))
    _custom_webhook({"event": "suggestions", "count": n,
                     "url": f"{base_url}/suggestions" if base_url else ""})
    return sent
