"""Invitaciones por enlace: quien administra crea una cuenta en Navidrome y manda un enlace; quien lo recibe abre
Melvara, elige su contraseña y entra sola.

- La app de quien administra crea la cuenta con una contraseña aleatoria (que nadie ve) y se la da a este servidor
  junto con la dirección pública de Navidrome. El servidor devuelve un código y el enlace.
- En el enlace solo va el código (aleatorio, 256 bits, de un solo uso, caduca a los 7 días). Ni el usuario ni la
  contraseña: los enlaces se quedan en los chats y en sus vistas previas.
- En la base de datos se guarda el hash del código y la contraseña temporal cifrada con una clave que sale del
  propio código: con la base de datos sola no se puede sacar.
- Al canjearlo, el servidor entra en Navidrome con la contraseña temporal y pone la que ha elegido la persona
  (como «Cambiar contraseña» en la app). Solo entonces se gasta el código y se borra la temporal.
- Abrir la página del enlace (o su vista previa en WhatsApp) no gasta nada.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time

import httpx

log = logging.getLogger("melvara.invites")

TTL = 7 * 86400
MAX_PENDING = 50


def _hash(code: str) -> str:
    return hashlib.sha256(f"melvara-invite|{code}".encode()).hexdigest()


def _keystream(code: str, length: int) -> bytes:
    key = hmac.new(code.encode(), b"melvara-invite-password", hashlib.sha256).digest()
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(key + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]


def _seal(code: str, password: str) -> str:
    data = password.encode()
    return bytes(a ^ b for a, b in zip(data, _keystream(code, len(data)))).hex()


def _open(code: str, sealed: str) -> str:
    data = bytes.fromhex(sealed)
    return bytes(a ^ b for a, b in zip(data, _keystream(code, len(data)))).decode()


class InviteError(ValueError):
    pass


class Invites:
    def __init__(self, path: str, navidrome_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.navidrome_url = navidrome_url.rstrip("/")
        self.transport = transport
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code_hash TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    sealed_password TEXT,
                    server TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                )"""
            )
            self._db.commit()

    @staticmethod
    def _row(r) -> dict:
        return {"id": r[0], "username": r[1], "server": r[2], "createdBy": r[3], "createdAt": r[4],
                "expiresAt": r[5], "usedAt": r[6]}

    def _cleanup(self) -> None:
        """Las caducadas pierden la contraseña temporal; las de hace más de 30 días se borran."""
        now = int(time.time())
        self._db.execute("UPDATE invites SET sealed_password = NULL WHERE expires_at < ?", (now,))
        self._db.execute("DELETE FROM invites WHERE expires_at < ?", (now - 30 * 86400,))

    def create(self, username: str, password: str, server: str, by: str) -> tuple[dict, str]:
        username, server = username.strip(), server.strip().rstrip("/")
        if not username or len(username) > 100:
            raise InviteError("Falta el usuario")
        if not 8 <= len(password) <= 128:
            raise InviteError("La contraseña temporal debe tener entre 8 y 128 caracteres")
        if not server.startswith("https://") or len(server) > 300:
            raise InviteError("La dirección de Navidrome debe empezar por https://")
        code = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._lock:
            self._cleanup()
            pending = self._db.execute("SELECT COUNT(*) FROM invites WHERE used_at IS NULL AND expires_at > ?",
                                       (now,)).fetchone()[0]
            if pending >= MAX_PENDING:
                raise InviteError(f"Como mucho {MAX_PENDING} invitaciones pendientes")
            # Una invitación nueva para el mismo usuario sustituye a la anterior.
            self._db.execute("DELETE FROM invites WHERE username = ? COLLATE NOCASE AND used_at IS NULL", (username,))
            cur = self._db.execute(
                "INSERT INTO invites (code_hash, username, sealed_password, server, created_by, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_hash(code), username, _seal(code, password), server, by, now, now + TTL))
            self._db.commit()
            row = self._db.execute(
                "SELECT id, username, server, created_by, created_at, expires_at, used_at FROM invites WHERE id = ?",
                (cur.lastrowid,)).fetchone()
        return self._row(row), code

    def pending(self) -> list[dict]:
        now = int(time.time())
        with self._lock:
            self._cleanup()
            self._db.commit()
            rows = self._db.execute(
                "SELECT id, username, server, created_by, created_at, expires_at, used_at FROM invites "
                "WHERE used_at IS NULL AND expires_at > ? ORDER BY created_at DESC", (now,)).fetchall()
        return [self._row(r) for r in rows]

    def cancel(self, invite_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM invites WHERE id = ? AND used_at IS NULL", (invite_id,))
            self._db.commit()
        return cur.rowcount > 0

    def _find(self, code: str):
        if not code or len(code) > 100:
            return None
        with self._lock:
            return self._db.execute(
                "SELECT id, username, server, created_by, created_at, expires_at, used_at, sealed_password "
                "FROM invites WHERE code_hash = ?", (_hash(code),)).fetchone()

    def check(self, code: str) -> dict:
        """Lo que dice el enlace (sin gastarlo): usuario y servidor."""
        row = self._find(code)
        if row is None:
            raise LookupError("Esta invitación no existe o ya no vale. Pide otra a quien administra el servidor.")
        if row[6] is not None:
            raise LookupError("Esta invitación ya se ha usado. Si eras tú, entra con tu usuario y tu contraseña.")
        if row[5] < time.time() or not row[7]:
            raise LookupError("Esta invitación ha caducado. Pide otra a quien administra el servidor.")
        return {"username": row[1], "server": row[2], "expiresAt": row[5]}

    async def redeem(self, code: str, new_password: str) -> dict:
        """Pone la contraseña elegida en Navidrome y gasta el código. Devuelve usuario y servidor."""
        info = self.check(code)
        if not 4 <= len(new_password) <= 128:
            raise InviteError("La contraseña debe tener al menos 4 caracteres")
        row = self._find(code)
        temp = _open(code, row[7])
        await self._change_password(info["username"], temp, new_password)
        with self._lock:
            self._db.execute("UPDATE invites SET used_at = ?, sealed_password = NULL WHERE id = ?",
                             (int(time.time()), row[0]))
            self._db.commit()
        log.info("Invitación %d usada por %s", row[0], info["username"])
        return {"username": info["username"], "server": info["server"]}

    async def _change_password(self, username: str, current: str, new: str) -> None:
        """Como «Cambiar contraseña» en la app: entrar en la API de Navidrome y guardar el usuario con la nueva."""
        async with httpx.AsyncClient(base_url=self.navidrome_url, timeout=20.0, transport=self.transport) as client:
            try:
                login = await client.post("/auth/login", json={"username": username, "password": current})
            except httpx.HTTPError:
                raise InviteError("Navidrome no responde ahora mismo. Inténtalo en un momento.") from None
            if login.status_code == 401:
                raise InviteError("La cuenta ya no tiene la contraseña de la invitación (¿ya la has cambiado?). "
                                  "Pide otra invitación.")
            if login.status_code != 200:
                raise InviteError(f"Navidrome no aceptó el inicio de sesión (código {login.status_code})")
            data = login.json()
            headers = {"X-ND-Authorization": f"Bearer {data['token']}"}
            user = (await client.get(f"/api/user/{data['id']}", headers=headers)).json()
            user["currentPassword"] = current
            user["password"] = new
            saved = await client.put(f"/api/user/{data['id']}", json=user, headers=headers)
            if not 200 <= saved.status_code < 300:
                raise InviteError(f"Navidrome no guardó la contraseña (código {saved.status_code})")


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><meta property="og:title" content="Invitación a Melvara">
<meta property="og:description" content="Abre el enlace en tu iPhone para entrar en Melvara."><title>{title}</title>
<style>
:root{{--bg:#f6f5f2;--card:#fff;--ink:#1d1b19;--mute:#6b6660;--line:#e4e0da;--acc:#e8365d}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151413;--card:#1f1e1c;--ink:#f1eee9;--mute:#a39d95;--line:#34312d;--acc:#ff5c7f}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:480px;margin:0 auto;padding:40px 16px}}section{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px}}
h1{{font-size:22px;margin:0 0 8px}}p{{margin:8px 0}}.mute{{color:var(--mute);font-size:14px}}
a.button{{display:block;text-align:center;padding:14px;border-radius:12px;background:var(--acc);color:#fff;text-decoration:none;font-weight:600;margin:16px 0 8px}}
a.second{{background:transparent;color:var(--acc);border:1px solid var(--acc)}}
</style></head><body><main><section>{body}</section></main></body></html>"""
