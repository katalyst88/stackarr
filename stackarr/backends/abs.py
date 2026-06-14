"""Audiobookshelf as a Backend. Thin adapter over the existing `absclient`
module — the long-standing ABS behaviour is unchanged; this just exposes it
through the pluggable Backend surface so the scheduler/recommender can treat
ABS as one source among (eventually) several. ABS remains the login backend
and the audiobook source."""
from __future__ import annotations

from .. import absclient
from .base import Backend


class ABSBackend(Backend):
    id = "abs"
    label = "Audiobookshelf"
    media_format = "audiobook"
    is_login = True
    supports_progress = True
    can_login = True

    can_import_users = True

    def verify_login(self, username: str, password: str) -> dict | None:
        info = absclient.login(username, password)
        if not info:
            return None
        return {"external_id": info.get("id") or username, "username": info.get("username") or username,
                "token": info.get("token", ""), "is_admin": bool(info.get("isAdmin"))}

    def list_users(self) -> list[dict]:
        return [{"external_id": u["id"] or u["username"], "username": u["username"],
                 "email": u.get("email", ""), "is_admin": u["is_admin"]}
                for u in absclient.list_users()]

    # --- connection -------------------------------------------------------
    def enabled(self) -> bool:
        return bool(absclient.abs_url() and absclient.admin_token())

    def test(self) -> dict:
        try:
            libs = absclient.libraries()
            n = len(libs)
            return {"ok": True,
                    "detail": f"Connected — {n} book librar{'y' if n == 1 else 'ies'}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def login(self, username: str, password: str) -> dict | None:
        return absclient.login(username, password)

    # --- data -------------------------------------------------------------
    def library_items(self) -> list[dict]:
        out = []
        for lib in absclient.libraries():
            try:
                for it in absclient.items(lib["id"]):
                    m = absclient.item_meta(it)
                    if not m.get("item_id"):
                        continue
                    m["library_id"] = lib["id"]
                    out.append(self._tag(m))
            except Exception:
                continue
        return out

    def reading_history(self, user: dict) -> list[dict]:
        token = (user or {}).get("abs_token")
        return absclient.listening_history(token) if token else []

    def listening_stats(self, user: dict) -> dict:
        token = (user or {}).get("abs_token")
        return absclient.listening_stats(token) if token else super().listening_stats(user)
