"""
Autenticação por usuário (login + senha) com sessão em cookie.

- Usuários e sessões em SQLite próprio (`auth.db`). Senhas guardadas com hash
  pbkdf2-sha256 + salt por usuário (nunca em texto puro; stdlib, sem dependência).
- Sessão = token opaco (cookie httponly) com validade; logout apaga o token.
- Modelo escolhido: administrador gerencia os logins (sem auto-cadastro); no 1º
  acesso, quando não há nenhum usuário, cria-se o primeiro admin pela tela de login.
- Todos os usuários logados veem todos os painéis (sem permissão por tópico) —
  `is_admin` só controla quem pode gerenciar usuários.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

DB_PATH = os.getenv("AUTH_DB", str(Path(__file__).parent.parent / "tables" / "auth.db"))

COOKIE_NAME = "volix_session"
SESSION_HORAS = int(os.getenv("AUTH_SESSION_HORAS", "12"))
_PBKDF2_ITER = 200_000

# Áreas de acesso (espelham os tópicos da sidebar). `areas` do usuário guarda a
# lista das que ele pode ver; o valor especial "*" = todas. Admin ignora tudo.
AREAS_KEYS = ("financeiro", "expedicao", "suprimentos", "entregas", "comercial", "config")
# Capacidades de ação (toggles por usuário). Admin ignora (pode tudo).
CAPACIDADES = ("pode_exportar", "pode_importar", "pode_editar_cadastro")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS usuarios (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            login         TEXT NOT NULL UNIQUE,
            nome          TEXT DEFAULT '',
            senha_hash    TEXT NOT NULL,
            salt          TEXT NOT NULL,
            is_admin      INTEGER DEFAULT 0,
            ativo         INTEGER DEFAULT 1,
            criado_em     TEXT,
            atualizado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            criado_em  TEXT,
            expira_em  TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_sessions_usuario ON sessions(usuario_id);
        """
    )
    _ensure_columns(conn)
    conn.commit()
    conn.close()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Migração leve: adiciona as colunas de permissão em bancos já existentes.
    Usuários anteriores herdam 'ver todas as áreas' (areas='*') e nenhuma ação
    (comportamento seguro) — o admin, sendo superusuário, continua com tudo."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(usuarios)").fetchall()}
    if "pode_exportar" not in cols:
        conn.execute("ALTER TABLE usuarios ADD COLUMN pode_exportar INTEGER DEFAULT 0")
    if "pode_importar" not in cols:
        conn.execute("ALTER TABLE usuarios ADD COLUMN pode_importar INTEGER DEFAULT 0")
    if "pode_editar_cadastro" not in cols:
        conn.execute("ALTER TABLE usuarios ADD COLUMN pode_editar_cadastro INTEGER DEFAULT 0")
    if "areas" not in cols:
        conn.execute("ALTER TABLE usuarios ADD COLUMN areas TEXT DEFAULT '*'")
        conn.execute("UPDATE usuarios SET areas='*' WHERE areas IS NULL OR areas=''")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ─── Senhas ──────────────────────────────────────────────────────────────────

def _hash_senha(senha: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITER).hex()
    return salt, h


def _login_norm(login: str) -> str:
    return (login or "").strip().lower()


def _parse_areas(raw: Optional[str]) -> List[str]:
    """String do banco -> lista de áreas. '*' = todas; '' = nenhuma (só a Home)."""
    raw = (raw or "").strip()
    if raw == "*":
        return ["*"]
    if raw == "":
        return []
    return [a for a in raw.split(",") if a in AREAS_KEYS]


def _areas_to_str(areas: Optional[List[str]]) -> str:
    """Lista -> string do banco. None/['*'] = todas ('*')."""
    if areas is None or "*" in areas:
        return "*"
    return ",".join(a for a in areas if a in AREAS_KEYS)


def _row_to_user(row: sqlite3.Row) -> Dict:
    """Sem expor senha_hash/salt. Inclui permissões (áreas + ações)."""
    chaves = row.keys()
    return {
        "id": row["id"], "login": row["login"], "nome": row["nome"],
        "is_admin": bool(row["is_admin"]), "ativo": bool(row["ativo"]),
        "pode_exportar": bool(row["pode_exportar"]) if "pode_exportar" in chaves else False,
        "pode_importar": bool(row["pode_importar"]) if "pode_importar" in chaves else False,
        "pode_editar_cadastro": bool(row["pode_editar_cadastro"]) if "pode_editar_cadastro" in chaves else False,
        "areas": _parse_areas(row["areas"]) if "areas" in chaves else ["*"],
        "criado_em": row["criado_em"], "atualizado_em": row["atualizado_em"],
    }


# ─── Checagem de permissão (admin ignora tudo — é superusuário) ───────────────

def pode(user: Optional[Dict], capacidade: str) -> bool:
    if not user:
        return False
    if user.get("is_admin"):
        return True
    return bool(user.get(capacidade))


def ve_area(user: Optional[Dict], area: str) -> bool:
    if not user:
        return False
    if user.get("is_admin"):
        return True
    areas = user.get("areas") or []
    return "*" in areas or area in areas


# ─── Usuários ────────────────────────────────────────────────────────────────

def count_users() -> int:
    conn = _get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
    finally:
        conn.close()


def has_users() -> bool:
    return count_users() > 0


def create_user(login: str, senha: str, nome: str = "", is_admin: bool = False,
                pode_exportar: bool = False, pode_importar: bool = False,
                pode_editar_cadastro: bool = False, areas: Optional[List[str]] = None) -> Dict:
    login = _login_norm(login)
    if not login:
        raise ValueError("Login é obrigatório.")
    if not senha or len(senha) < 4:
        raise ValueError("A senha deve ter ao menos 4 caracteres.")
    salt, h = _hash_senha(senha)
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO usuarios (login, nome, senha_hash, salt, is_admin, ativo, "
            "pode_exportar, pode_importar, pode_editar_cadastro, areas, criado_em, atualizado_em) "
            "VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
            (login, (nome or "").strip(), h, salt, 1 if is_admin else 0,
             1 if pode_exportar else 0, 1 if pode_importar else 0, 1 if pode_editar_cadastro else 0,
             _areas_to_str(areas), _now(), _now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM usuarios WHERE id=?", (cur.lastrowid,)).fetchone()
        return _row_to_user(row)
    finally:
        conn.close()


def list_users() -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM usuarios ORDER BY login").fetchall()
        return [_row_to_user(r) for r in rows]
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def update_user(user_id: int, nome: Optional[str] = None, is_admin: Optional[bool] = None,
                ativo: Optional[bool] = None, senha: Optional[str] = None,
                pode_exportar: Optional[bool] = None, pode_importar: Optional[bool] = None,
                pode_editar_cadastro: Optional[bool] = None, areas: Optional[List[str]] = None) -> Optional[Dict]:
    conn = _get_conn()
    try:
        atual = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
        if atual is None:
            return None
        sets, params = [], []
        if nome is not None:
            sets.append("nome=?"); params.append(nome.strip())
        if is_admin is not None:
            sets.append("is_admin=?"); params.append(1 if is_admin else 0)
        if ativo is not None:
            sets.append("ativo=?"); params.append(1 if ativo else 0)
        if pode_exportar is not None:
            sets.append("pode_exportar=?"); params.append(1 if pode_exportar else 0)
        if pode_importar is not None:
            sets.append("pode_importar=?"); params.append(1 if pode_importar else 0)
        if pode_editar_cadastro is not None:
            sets.append("pode_editar_cadastro=?"); params.append(1 if pode_editar_cadastro else 0)
        if areas is not None:
            sets.append("areas=?"); params.append(_areas_to_str(areas))
        if senha:
            if len(senha) < 4:
                raise ValueError("A senha deve ter ao menos 4 caracteres.")
            salt, h = _hash_senha(senha)
            sets += ["senha_hash=?", "salt=?"]; params += [h, salt]
        sets.append("atualizado_em=?"); params.append(_now())
        params.append(user_id)
        conn.execute(f"UPDATE usuarios SET {', '.join(sets)} WHERE id=?", params)
        # Desativar/rebaixar → encerra as sessões desse usuário
        if ativo is False:
            conn.execute("DELETE FROM sessions WHERE usuario_id=?", (user_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
        return _row_to_user(row)
    finally:
        conn.close()


def delete_user(user_id: int) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE usuario_id=?", (user_id,))
        cur = conn.execute("DELETE FROM usuarios WHERE id=?", (user_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_admins(excluir_id: Optional[int] = None) -> int:
    """Admins ATIVOS. Usado para impedir remover/rebaixar o último admin."""
    conn = _get_conn()
    try:
        q = "SELECT COUNT(*) FROM usuarios WHERE is_admin=1 AND ativo=1"
        params: list = []
        if excluir_id is not None:
            q += " AND id<>?"; params.append(excluir_id)
        return conn.execute(q, params).fetchone()[0]
    finally:
        conn.close()


# ─── Autenticação e sessões ──────────────────────────────────────────────────

def verify_credentials(login: str, senha: str) -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM usuarios WHERE login=?", (_login_norm(login),)).fetchone()
    finally:
        conn.close()
    if row is None or not row["ativo"]:
        return None
    _, h = _hash_senha(senha, row["salt"])
    if not secrets.compare_digest(h, row["senha_hash"]):
        return None
    return _row_to_user(row)


def create_session(usuario_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expira = (datetime.now() + timedelta(hours=SESSION_HORAS)).isoformat(timespec="seconds")
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE expira_em < ?", (_now(),))  # limpa expiradas
        conn.execute("INSERT INTO sessions (token, usuario_id, criado_em, expira_em) VALUES (?,?,?,?)",
                     (token, usuario_id, _now(), expira))
        conn.commit()
    finally:
        conn.close()
    return token


def get_session_user(token: Optional[str]) -> Optional[Dict]:
    if not token:
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN usuarios u ON u.id = s.usuario_id "
            "WHERE s.token=? AND s.expira_em > ? AND u.ativo=1",
            (token, _now()),
        ).fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def delete_session(token: Optional[str]) -> None:
    if not token:
        return
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()
