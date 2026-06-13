"""Authentication: multi-user via Audiobookshelf credentials. Users sign in
with their ABS username/password; Stackarr verifies against ABS, stores the
returned token (to read that user's history), and tracks role. Admins (set
via STACKARR_ADMINS, or ABS admins) can see all queues and auto-approve."""
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from . import absclient, config, db


def current_user() -> dict | None:
    uid = session.get("uid")
    return db.get_user(uid) if uid else None


def do_login(username: str, password: str) -> dict | None:
    info = absclient.login(username, password)
    if not info:
        return None
    role = "admin" if (info["isAdmin"] or info["username"] in config.ADMIN_USERS) else "user"
    user = db.upsert_user(info["id"], info["username"], info["token"], role)
    session.permanent = True
    session["uid"] = user["id"]
    return user


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
