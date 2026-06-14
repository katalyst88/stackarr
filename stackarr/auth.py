"""Authentication. A Stackarr account is a local identity (username, optional
password). Users can sign in either with a local password OR with an external
library account (Audiobookshelf / Kavita / Komga / Calibre-Web) — every external
sign-in is find-or-created and **linked to a local account**, so the local
account is always the canonical identity. Admins (first user, STACKARR_ADMINS,
or an admin on the external source) see all queues and can manage settings."""
import threading
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from . import backends, config, db


def current_user() -> dict | None:
    uid = session.get("uid")
    return db.get_user(uid) if uid else None


def _start_session(user: dict):
    session.permanent = True
    session["uid"] = user["id"]
    # First login (never run before) -> generate picks immediately in the
    # background so the suggestions page is populated within seconds.
    if not db.get_meta(f"suggest_run_{user['id']}"):
        def _first_run(uid):
            from . import scheduler
            scheduler.run_for_user(uid, force=True)
        threading.Thread(target=_first_run, args=(user["id"],), daemon=True).start()


def login_providers() -> list[dict]:
    """Sign-in methods to offer: a local password, plus each connected source
    that can authenticate. Order: Audiobookshelf first, then other sources."""
    out = []
    for b in backends.ALL:
        if not getattr(b, "can_login", False):
            continue
        try:
            if b.enabled():
                out.append({"id": b.id, "label": b.label})
        except Exception:
            continue
    return out


def _role_for(username: str, provider_admin: bool, verified: bool) -> str:
    """Decide a new account's role. First account ever = admin (bootstrap). After
    that, admin only when the identity is VERIFIED by an external source — the
    source said this user is an admin, or the (source-authenticated) username is in
    the operator's ADMIN_USERS list. A self-chosen local signup is NEVER admin via
    ADMIN_USERS, otherwise anyone could register an admin's username to escalate."""
    if db.user_count() == 0:
        return "admin"
    if verified and (provider_admin or username in config.ADMIN_USERS):
        return "admin"
    return "user"


def do_login_local(username: str, password: str) -> dict | None:
    u = db.verify_local(username, password)
    if u:
        _start_session(u)
    return u


def do_login_provider(provider_id: str, username: str, password: str) -> dict | None:
    be = backends.by_id(provider_id)
    if not be or not getattr(be, "can_login", False):
        return None
    info = be.verify_login(username, password)
    if not info:
        return None
    # identity came from the external source, so ADMIN_USERS/admin flags are trusted
    role = _role_for(info["username"], info.get("is_admin", False), verified=True)
    u = db.provision_provider_user(provider_id, info["external_id"], info["username"],
                                   info.get("token", ""), role)
    if provider_id == "abs":
        db.update_abs(u["id"], info["external_id"], info.get("token", ""))
        u = db.get_user(u["id"])
    _start_session(u)
    return u


def register_local(username: str, password: str, email: str = "") -> dict | None:
    # self-chosen signup: only the very first account is admin (bootstrap); a
    # username match against ADMIN_USERS does NOT grant admin here (unverified).
    role = _role_for(username, False, verified=False)
    u = db.create_local_user(username, password, role=role, email=email)
    if u:
        _start_session(u)
    return u


def link_provider(user: dict, provider_id: str, username: str, password: str) -> bool:
    """Attach an external provider to the logged-in account (from Settings)."""
    be = backends.by_id(provider_id)
    if not be or not getattr(be, "can_login", False):
        return False
    info = be.verify_login(username, password)
    if not info:
        return False
    owner = db.link_get(provider_id, info["external_id"])
    if owner and owner != user["id"]:
        return False            # that identity already belongs to another account
    db.link_set(provider_id, info["external_id"], user["id"], info.get("token", ""))
    if provider_id == "abs":
        db.update_abs(user["id"], info["external_id"], info.get("token", ""))
    return True


def login_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if current_user():
            return f(*a, **kw)
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("main.login", next=request.path))
    return wrapped


def admin_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        u = current_user()
        if not u:
            return jsonify({"error": "unauthorized"}), 401
        if u["role"] != "admin":
            return jsonify({"error": "forbidden"}), 403
        return f(*a, **kw)
    return wrapped
