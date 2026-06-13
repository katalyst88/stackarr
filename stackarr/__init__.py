"""Stackarr app factory. Configures Flask for optional subpath/iframe use
(so it embeds cleanly in an nzb360 webview or behind a reverse proxy),
loads the DB, and starts the background worker."""
import logging
import sys
from datetime import timedelta

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, config, db, scheduler


class _PrefixMiddleware:
    """Serve the app under URL_BASE without baking the prefix into routes."""
    def __init__(self, app, prefix=""):
        self.app, self.prefix = app, prefix

    def __call__(self, environ, start_response):
        if self.prefix and environ.get("PATH_INFO", "").startswith(self.prefix):
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = environ["PATH_INFO"][len(self.prefix):] or "/"
        return self.app(environ, start_response)


def _setup_logging():
    import os
    from logging.handlers import RotatingFileHandler
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.handlers.clear()
    console = logging.StreamHandler(); console.setFormatter(fmt); root.addHandler(console)
    fileh = RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fileh.setFormatter(fmt); root.addHandler(fileh)


def create_app() -> Flask:
    _setup_logging()
    problems = config.validate()
    if problems:
        for p in problems:
            logging.error("config: %s", p)
        sys.exit(1)

    db.init()
    app = Flask(__name__)
    app.secret_key = db.secret_key()
    app.permanent_session_lifetime = timedelta(days=90)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    if config.URL_BASE:
        app.wsgi_app = _PrefixMiddleware(app.wsgi_app, config.URL_BASE)

    # Allow embedding in nzb360 / dashboards (override Flask's default deny).
    @app.after_request
    def _frame_friendly(resp):
        resp.headers.pop("X-Frame-Options", None)
        resp.headers["Content-Security-Policy"] = "frame-ancestors *"
        return resp

    from .routes import bp
    app.register_blueprint(bp)

    @app.context_processor
    def _inject():
        return {"accent": config.ACCENT, "app_name": config.APP_NAME,
                "url_base": config.URL_BASE, "user": auth.current_user(),
                "version": config.VERSION, "stage": config.RELEASE_STAGE}

    scheduler.start()
    return app
