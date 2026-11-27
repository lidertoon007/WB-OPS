"""Команда: приглашения по одноразовому коду и входы сотрудников.

Владелец работает на компьютере, где запущен WB OPS (запросы с 127.0.0.1), и входит без кода.
Сотрудник открывает ссылку-приглашение по локальной сети, вводит имя — и получает свой вход
(cookie на 180 дней). В базе хранится только хэш ключа входа. Доступ можно отключить в любой момент.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import socket
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

COOKIE = "wbops_member"
COOKIE_DAYS = 180
INVITE_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def lan_ip() -> str:
    """IP этого компьютера в локальной сети (без отправки пакетов)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


class Team:
    def __init__(self, store):
        self.store = store
        with self.store._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS team_invites (code TEXT PRIMARY KEY, author TEXT NOT NULL, created_at TEXT NOT NULL, "
                      "expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', used_by INTEGER, used_at TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS team_members (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '', "
                      "token_hash TEXT NOT NULL UNIQUE, invite_code TEXT, created_at TEXT NOT NULL, last_seen TEXT, active INTEGER NOT NULL DEFAULT 1)")
            columns = {r[1] for r in c.execute("PRAGMA table_info(team_members)")}
            for name, definition in (("login", "TEXT"), ("password_hash", "TEXT"), ("role", "TEXT NOT NULL DEFAULT 'member'")):
                if name not in columns:
                    c.execute(f"ALTER TABLE team_members ADD COLUMN {name} {definition}")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS team_login ON team_members(login COLLATE NOCASE)")
            c.execute("CREATE TABLE IF NOT EXISTS team_sessions (token_hash TEXT PRIMARY KEY, member_id INTEGER NOT NULL, expires_at TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS team_login_attempts (login TEXT PRIMARY KEY, failures INTEGER NOT NULL, started_at TEXT NOT NULL)")

    def create_user(self, login: str, password: str, name: str = "", role: str = "member") -> dict:
        login = str(login or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.@-]{2,63}", login):
            raise ValueError("Логин: 3–64 символа, латинские буквы, цифры, . _ @ -")
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError("Пароль должен содержать от 12 до 256 символов")
        if role not in ("owner", "member"):
            raise ValueError("Неизвестная роль")
        name = " ".join(str(name or login).split())[:80]
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
        now = _now().isoformat()
        try:
            with self.store._connect() as c:
                member_id = c.execute("INSERT INTO team_members(name,login,password_hash,role,token_hash,created_at) VALUES(?,?,?,?,?,?)",
                                      (name, login, f"pbkdf2_sha256$600000${salt}${digest}", role, _hash(secrets.token_urlsafe(32)), now)).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("Этот логин уже занят") from None
        return self.member(member_id)

    def login(self, login: str, password: str) -> tuple[dict, str]:
        login = str(login or "").strip().lower()[:64]
        now = _now()
        with self.store._connect() as c:
            attempt = c.execute("SELECT failures,started_at FROM team_login_attempts WHERE login=?", (login,)).fetchone()
            if attempt and now - datetime.fromisoformat(attempt[1]) < timedelta(minutes=15) and attempt[0] >= 5:
                raise ValueError("Слишком много попыток. Повторите через 15 минут")
            # Reserve an attempt before hashing; limit works across workers and simultaneous requests.
            if not attempt or now - datetime.fromisoformat(attempt[1]) >= timedelta(minutes=15):
                c.execute("INSERT OR REPLACE INTO team_login_attempts VALUES(?,1,?)", (login, now.isoformat()))
            else:
                c.execute("UPDATE team_login_attempts SET failures=failures+1 WHERE login=?", (login,))
            row = c.execute("SELECT id,password_hash FROM team_members WHERE login=? COLLATE NOCASE AND active=1", (login,)).fetchone()
        encoded = row[1] if row and row[1] else "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32
        _, iterations, salt, expected = encoded.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", str(password or "")[:256].encode(), bytes.fromhex(salt), int(iterations)).hex()
        if not row or not hmac.compare_digest(expected, actual):
            raise ValueError("Неверный логин или пароль")
        token = secrets.token_urlsafe(32)
        with self.store._connect() as c:
            c.execute("DELETE FROM team_login_attempts WHERE login=?", (login,))
            c.execute("DELETE FROM team_sessions WHERE expires_at<?", (now.isoformat(),))
            c.execute("INSERT INTO team_sessions VALUES(?,?,?)", (_hash(token), row[0], (now + timedelta(days=COOKIE_DAYS)).isoformat()))
        return self.member(row[0]), token

    def logout(self, token: str | None) -> None:
        if token:
            with self.store._connect() as c:
                c.execute("DELETE FROM team_sessions WHERE token_hash=?", (_hash(token),))
                # Revoke an old invitation cookie as well.
                c.execute("UPDATE team_members SET token_hash=? WHERE token_hash=?", (_hash(secrets.token_urlsafe(32)), _hash(token)))

    def change_password(self, member_id: int, current_password: str, password: str) -> None:
        member = self.member(member_id)
        if not member or not member.get("login"):
            raise ValueError("Учётная запись не найдена")
        _, verification_token = self.login(member["login"], current_password)
        self.logout(verification_token)
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError("Пароль должен содержать от 12 до 256 символов")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
        with self.store._connect() as c:
            c.execute("UPDATE team_members SET password_hash=? WHERE id=?", (f"pbkdf2_sha256$600000${salt}${digest}", member_id))
            c.execute("DELETE FROM team_sessions WHERE member_id=?", (member_id,))

    # ------------------------------------------------------------------ приглашения
    def create_invite(self, author: str, days: int = INVITE_DAYS) -> dict:
        code = str(uuid.uuid4())
        now = _now()
        with self.store._connect() as c:
            c.execute("INSERT INTO team_invites(code,author,created_at,expires_at) VALUES(?,?,?,?)",
                      (code, author or "Владелец", now.isoformat(), (now + timedelta(days=max(1, min(days, 30)))).isoformat()))
        return self.invite(code)

    @staticmethod
    def _status(row) -> str:
        status = row["status"]
        if status == "active" and datetime.fromisoformat(row["expires_at"]) < _now():
            return "expired"
        return status

    def invite(self, code: str) -> dict | None:
        with self.store._connect() as c:
            c.row_factory = __import__("sqlite3").Row
            row = c.execute("SELECT i.*, m.name AS member_name FROM team_invites i LEFT JOIN team_members m ON m.id=i.used_by WHERE code=?", (code,)).fetchone()
        return self._invite_dict(row) if row else None

    def _invite_dict(self, row) -> dict:
        return {"code": row["code"], "author": row["author"], "created_at": row["created_at"], "expires_at": row["expires_at"],
                "status": self._status(row), "used_by": row["member_name"], "used_at": row["used_at"]}

    def invites(self) -> list[dict]:
        with self.store._connect() as c:
            c.row_factory = __import__("sqlite3").Row
            rows = c.execute("SELECT i.*, m.name AS member_name FROM team_invites i LEFT JOIN team_members m ON m.id=i.used_by "
                             "ORDER BY i.created_at DESC").fetchall()
        return [self._invite_dict(r) for r in rows]

    def delete_invite(self, code: str) -> bool:
        with self.store._connect() as c:
            return c.execute("DELETE FROM team_invites WHERE code=?", (code,)).rowcount > 0

    # ------------------------------------------------------------------ сотрудники
    def join(self, code: str, name: str, email: str = "") -> tuple[dict, str]:
        """Принять приглашение: вернуть (сотрудник, ключ входа). Ключ показывается только здесь."""
        name = " ".join(str(name or "").split())[:80]
        email = str(email or "").strip()[:120]
        if len(name) < 2:
            raise ValueError("Укажите имя (не короче 2 букв)")
        if email and "@" not in email:
            raise ValueError("Проверьте email")
        invite = self.invite(str(code or "").strip())
        if not invite:
            raise ValueError("Код приглашения не найден")
        if invite["status"] != "active":
            raise ValueError({"expired": "Срок приглашения истёк — попросите новую ссылку",
                              "used": "Это приглашение уже использовано — попросите новую ссылку"}.get(invite["status"], "Приглашение недействительно"))
        token = secrets.token_urlsafe(32)
        now = _now().isoformat()
        with self.store._connect() as c:
            # Код одноразовый: помечаем «использован» только если он ещё активен (защита от двойного входа)
            if c.execute("UPDATE team_invites SET status='used', used_at=? WHERE code=? AND status='active'", (now, invite["code"])).rowcount != 1:
                raise ValueError("Это приглашение уже использовано")
            member_id = c.execute("INSERT INTO team_members(name,email,token_hash,invite_code,created_at,last_seen) VALUES(?,?,?,?,?,?)",
                                  (name, email, _hash(token), invite["code"], now, now)).lastrowid
            c.execute("UPDATE team_invites SET used_by=? WHERE code=?", (member_id, invite["code"]))
        return self.member(member_id), token

    def member(self, member_id: int) -> dict | None:
        with self.store._connect() as c:
            row = c.execute("SELECT id,name,email,created_at,last_seen,active,login,role FROM team_members WHERE id=?", (member_id,)).fetchone()
        return {"id": row[0], "name": row[1], "email": row[2], "created_at": row[3], "last_seen": row[4], "active": bool(row[5]), "login": row[6], "role": row[7]} if row else None

    def members(self) -> list[dict]:
        with self.store._connect() as c:
            rows = c.execute("SELECT id FROM team_members ORDER BY id").fetchall()
        return [self.member(r[0]) for r in rows]

    def by_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.store._connect() as c:
            row = c.execute("SELECT m.id,m.last_seen FROM team_members m LEFT JOIN team_sessions s ON s.member_id=m.id "
                            "WHERE m.active=1 AND ((s.token_hash=? AND s.expires_at>?) OR "
                            "(m.login IS NULL AND m.token_hash=? AND m.created_at>?)) LIMIT 1",
                            (_hash(token), _now().isoformat(), _hash(token), (_now() - timedelta(days=COOKIE_DAYS)).isoformat())).fetchone()
            if not row:
                return None
            now = _now()
            # last_seen обновляем не чаще раза в 5 минут, чтобы не писать в базу на каждый запрос
            if not row[1] or now - datetime.fromisoformat(row[1]) > timedelta(minutes=5):
                c.execute("UPDATE team_members SET last_seen=? WHERE id=?", (now.isoformat(), row[0]))
        return self.member(row[0])

    def set_active(self, member_id: int, active: bool) -> dict:
        with self.store._connect() as c:
            self._protect_last_owner(c, member_id, disabling=not active)
            if c.execute("UPDATE team_members SET active=? WHERE id=?", (int(active), member_id)).rowcount != 1:
                raise KeyError(member_id)
            if not active:
                c.execute("DELETE FROM team_sessions WHERE member_id=?", (member_id,))
                c.execute("UPDATE team_members SET token_hash=? WHERE id=?", (_hash(secrets.token_urlsafe(32)), member_id))
        return self.member(member_id)

    def delete_member(self, member_id: int) -> bool:
        with self.store._connect() as c:
            self._protect_last_owner(c, member_id, disabling=True)
            c.execute("DELETE FROM team_sessions WHERE member_id=?", (member_id,))
            return c.execute("DELETE FROM team_members WHERE id=?", (member_id,)).rowcount > 0

    @staticmethod
    def _protect_last_owner(c, member_id: int, disabling: bool) -> None:
        row = c.execute("SELECT role,active FROM team_members WHERE id=?", (member_id,)).fetchone()
        if disabling and row and row == ("owner", 1):
            if c.execute("SELECT COUNT(*) FROM team_members WHERE role='owner' AND active=1").fetchone()[0] <= 1:
                raise ValueError("Нельзя отключить последнего администратора")
