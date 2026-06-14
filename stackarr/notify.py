"""Notifications. Apprise is the engine (100+ channels for almost no code);
on top of it Stackarr ships a polished email digest with three selectable
themes (light / dark / fun) and a live preview, plus a simple Discord
webhook. All optional and per-deployment; toggled from Settings."""
import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape as _esc

import requests

from . import config, db


def _clean_subject(s: str) -> str:
    """Strip CR/LF so a crafted title can't inject extra email headers."""
    return (s or "").replace("\r", " ").replace("\n", " ")


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


def _send_email(subject: str, text: str, html: str, to: str | None = None) -> bool:
    s = smtp_settings()
    recipient = (to or s["to"]).strip()
    if not (s["host"] and recipient):
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, f"{config.APP_NAME} <{s['from']}>", recipient
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


def smtp_ready() -> bool:
    """SMTP is configured enough to send transactional mail to a given address
    (host present). The global 'to' isn't required for per-user mail."""
    return bool(smtp_settings()["host"])


def _card(heading: str, lines: list[str], base_url: str = "",
          btn_label: str = "", btn_path: str = "") -> str:
    """A small branded transactional email card (approval/availability mails)."""
    t = THEMES[current_theme()]
    body = "".join(f'<p style="margin:0 0 10px;color:{t["sub"]};font-size:14.5px;line-height:1.5;">{ln}</p>'
                   for ln in lines)
    btn = ""
    if base_url and btn_label:
        btn = (f'<a href="{base_url}{btn_path}" style="display:inline-block;background:{t["btnbg"]};'
               f'color:{t["btnfg"]};font-weight:700;text-decoration:none;padding:11px 26px;'
               f'border-radius:8px;font-size:14px;margin-top:6px;">{btn_label}</a>')
    return f"""<!doctype html><html><body style="margin:0;background:{t['page']};">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{t['page']};padding:28px 0;"><tr><td align="center">
<table width="520" cellpadding="0" cellspacing="0" style="background:{t['card']};border-radius:12px;overflow:hidden;max-width:94%;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
<tr><td style="background:{t['head']};padding:18px 26px;">
<span style="color:{t['brand']};font-size:18px;font-weight:700;">{config.APP_NAME}</span></td></tr>
<tr><td style="padding:22px 26px;">
<p style="margin:0 0 12px;color:{t['title']};font-size:16px;font-weight:700;">{heading}</p>
{body}{btn}</td></tr>
<tr><td style="background:{t['foot']};padding:12px 26px;color:{t['footfg']};font-size:12px;">
{config.APP_NAME} · manage email preferences in Settings.</td></tr>
</table></td></tr></table></body></html>"""


def request_pending(book: dict, requester: str, admin_emails: list[str], base_url: str = ""):
    """A user requested a book that needs admin approval — tell the admins.
    Dynamic strings (title/author/requester come from external metadata or a
    user-chosen name) are HTML-escaped so they can't inject markup into the mail."""
    raw_title = book.get("title", "a book")
    title, author, who = _esc(raw_title), _esc(book.get("author", "")), _esc(requester)
    fmt = "eBook" if book.get("format") == "ebook" else "audiobook"
    subject = _clean_subject(f"{config.APP_NAME}: approval needed — “{raw_title}”")
    lines = [f"<b>{who}</b> requested the {fmt} <b>{title}</b>{(' by ' + author) if author else ''}.",
             "It's waiting for your approval."]
    _discord(f"🕓 **{requester}** requested **{raw_title}**{(' — ' + book.get('author','')) if book.get('author') else ''} ({fmt}) — awaiting approval.")
    if smtp_ready():
        html = _card("A request needs your approval", lines, base_url, "Review requests", "/requests")
        text = f"{requester} requested {raw_title} ({fmt}). Awaiting approval."
        for em in admin_emails:
            _send_email(subject, text, html, to=em)


def request_approved(book: dict, to_email: str, base_url: str = ""):
    raw_title = book.get("title", "a book")
    title, author = _esc(raw_title), _esc(book.get("author", ""))
    if not (to_email and smtp_ready()):
        return
    subject = _clean_subject(f"{config.APP_NAME}: your request for “{raw_title}” was approved")
    lines = [f"Good news — your request for <b>{title}</b>{(' by ' + author) if author else ''} was approved.",
             "We're fetching it now; you'll get another note when it's ready."]
    _send_email(subject, f"Your request for {raw_title} was approved and is being fetched.",
                _card("Request approved 🎉", lines, base_url, "View your requests", "/requests"), to=to_email)


def request_denied(book: dict, to_email: str, reason: str = "", base_url: str = ""):
    raw_title = book.get("title", "a book")
    title, author = _esc(raw_title), _esc(book.get("author", ""))
    if not (to_email and smtp_ready()):
        return
    subject = _clean_subject(f"{config.APP_NAME}: your request for “{raw_title}” wasn't approved")
    lines = [f"Your request for <b>{title}</b>{(' by ' + author) if author else ''} wasn't approved."]
    if reason:
        lines.append(f"Reason: {_esc(reason)}")
    _send_email(subject, f"Your request for {raw_title} wasn't approved." + (f" {reason}" if reason else ""),
                _card("Request not approved", lines, base_url), to=to_email)


def request_available_user(book: dict, to_email: str, base_url: str = ""):
    """Per-user 'it's ready' mail — sent to the requester who owns the request."""
    raw_title = book.get("title", "a book")
    title, author = _esc(raw_title), _esc(book.get("author", ""))
    if not (to_email and smtp_ready()):
        return
    fmt = "eBook" if book.get("format") == "ebook" else "audiobook"
    subject = _clean_subject(f"{config.APP_NAME}: “{raw_title}” is ready")
    lines = [f"Your {fmt} <b>{title}</b>{(' by ' + author) if author else ''} just landed in the library — enjoy!"]
    _send_email(subject, f"{raw_title} is now available in your library.",
                _card("It's ready 📚", lines, base_url, "Open library", "/requests"), to=to_email)


def _row(s: dict, t: dict) -> str:
    # escape catalog-sourced strings (title/author/reason) and the cover URL
    cover = (f'<img src="{_esc(s["cover"], quote=True)}" width="56" height="56" style="border-radius:6px;'
             'object-fit:cover;display:block;">' if s.get("cover")
             else f'<div style="width:56px;height:56px;border-radius:6px;background:{t["tag"]};"></div>')
    return (f'<tr><td style="padding:10px 14px 10px 0;width:56px;vertical-align:top;">{cover}</td>'
            f'<td style="padding:10px 0;vertical-align:top;">'
            f'<div style="font-weight:600;color:{t["title"]};font-size:15px;">{_esc(s.get("title",""))}</div>'
            f'<div style="color:{t["sub"]};font-size:13px;">{_esc(s.get("author",""))}</div>'
            f'<div style="color:{t["reason"]};font-size:13px;font-style:italic;margin-top:3px;">{_esc(s.get("reason",""))}</div>'
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


def request_available(row: dict, base_url: str = ""):
    """A requested book just landed. Two audiences, kept distinct for multi-user:
      1. the **requester** gets a personal email (honouring their own toggle), and
      2. the admin's **global channels** (Discord / Apprise / custom webhook) fan
         out if the install-wide 'available' alert is on.
    `row` is a requests-table row (has user_id, title, author, format)."""
    book = {"title": row.get("title", "a book"), "author": row.get("author", ""),
            "format": row.get("format", "audiobook")}
    base = base_url or db.get_meta("public_url", "")
    # 1) personal email to whoever requested it
    uid = row.get("user_id")
    user = db.get_user(uid) if uid else None
    if user and user.get("email") and db.get_pref(uid, "notify_available", "1") == "1":
        request_available_user(book, user["email"], base)
    # 2) admin's global firehose channels (optional)
    if db.get_meta("notify_avail_enabled", "0") == "1":
        title, author = book["title"], book["author"]
        _discord(f"📚 **{title}**{(' — ' + author) if author else ''} is now in the library."
                 + (f" {base}" if base else ""))
        _apprise(f"{config.APP_NAME}: {title} is ready", f"{title} is now available.")
        _custom_webhook({"event": "available", "title": title, "author": author, "url": base})


def new_release(book: dict, base_url: str = ""):
    """Fire when an author the user reads puts out a new release (the
    follow-author 'new-release radar'). Master switch notify_newrelease_enabled;
    each channel self-gates."""
    if db.get_meta("notify_newrelease_enabled", "0") != "1":
        return
    title, author = book.get("title", "a book"), book.get("author", "")
    fmt = "eBook" if book.get("format") == "ebook" else "audiobook"
    base = base_url or db.get_meta("public_url", "")
    subject = f"{config.APP_NAME}: new from {author} — “{title}”"
    body = (f"{author} just released a new {fmt}: “{title}”"
            + (f" ({book['release_date']})" if book.get("release_date") else "") + ".")
    _discord(f"🆕 **{author}** released **{title}**" + (f" {base}" if base else ""))
    _apprise(subject, body)
    _custom_webhook({"event": "new_release", "title": title, "author": author,
                     "format": book.get("format", "audiobook"), "url": base})
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
