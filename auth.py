"""本地账号认证。密码只保存 scrypt 哈希，业务代码只接收服务端生成的 user_id。"""
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class AuthStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _validate(username, password):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
            raise ValueError("用户名须为 3 至 50 个字母、数字或 . _ -")
        if not 8 <= len(password) <= 128:
            raise ValueError("密码长度须为 8 至 128 个字符")

    @staticmethod
    def _hash(password, salt=None):
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()

    def register(self, username, password):
        username = username.strip()
        self._validate(username, password)
        user_id = str(uuid.uuid4())
        try:
            with self.connect() as db:
                db.execute("INSERT INTO users VALUES (?,?,?,?)", (user_id, username, self._hash(password), _now()))
        except sqlite3.IntegrityError as exc:
            raise ValueError("该用户名已被使用") from exc
        return {"id": user_id, "username": username}

    def authenticate(self, username, password):
        with self.connect() as db:
            row = db.execute("SELECT id,username,password_hash FROM users WHERE username=?", (username.strip(),)).fetchone()
        if not row:
            return None
        try:
            salt_raw, digest_raw = row[2].split("$", 1)
            expected = self._hash(password, base64.b64decode(salt_raw)).split("$", 1)[1]
        except Exception:
            return None
        if not hmac.compare_digest(expected, digest_raw):
            return None
        return {"id": row[0], "username": row[1]}
