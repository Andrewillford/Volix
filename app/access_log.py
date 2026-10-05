"""
Log de acesso / auditoria de segurança.

Registra QUEM fez O QUÊ e QUANDO nos pontos sensíveis do sistema:
entradas e saídas (login/logout, tentativas falhas), gestão de usuários,
edição da base de acordo (cadastro), importações, exportações de relatório
e acessos negados por falta de permissão.

Compartilha o mesmo SQLite da autenticação (`auth.db`) — é a mesma preocupação
(segurança) e evita mais um arquivo de banco. Só grava; nunca deleta sozinho
(há `purgar()` sob demanda para retenção). O registro é resiliente: uma falha
ao logar nunca deve derrubar a requisição que está sendo auditada.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Mesmo banco da autenticação (auth.db). Respeita a env AUTH_DB para os testes.
DB_PATH = os.getenv("AUTH_DB", str(Path(__file__).parent.parent / "tables" / "auth.db"))

# Eventos conhecidos -> rótulo amigável (usado no filtro e na tela).
EVENTOS = {
    "LOGIN_OK": "Login",
    "LOGIN_FALHA": "Login falhou",
    "LOGOUT": "Logout",
    "SETUP_ADMIN": "1º acesso (admin criado)",
    "USUARIO_CRIADO": "Usuário criado",
    "USUARIO_ALTERADO": "Usuário alterado",
    "USUARIO_EXCLUIDO": "Usuário excluído",
    "CADASTRO_EDITADO": "Cadastro editado",
    "AUDITORIA_APROVACAO": "Aprovação/reprovação de CTe",
    "IMPORTACAO": "Importação de arquivo",
    "EXPORTACAO": "Exportação (Excel)",
    "ACESSO_NEGADO": "Acesso negado",
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS access_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                evento     TEXT NOT NULL,
                usuario_id INTEGER,
                login      TEXT DEFAULT '',
                nome       TEXT DEFAULT '',
                detalhe    TEXT DEFAULT '',
                ip         TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_log_ts ON access_log(ts);
            CREATE INDEX IF NOT EXISTS ix_log_evento ON access_log(evento);
            CREATE INDEX IF NOT EXISTS ix_log_usuario ON access_log(usuario_id);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def registrar(evento: str, *, usuario_id: Optional[int] = None, login: str = "",
              nome: str = "", detalhe: str = "", ip: str = "") -> None:
    """Grava um evento de auditoria. Nunca levanta exceção para não interromper
    a requisição sendo auditada — só loga a falha no logger da aplicação."""
    try:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO access_log (ts, evento, usuario_id, login, nome, detalhe, ip) "
                "VALUES (?,?,?,?,?,?,?)",
                (_now(), evento, usuario_id, (login or "").strip(),
                 (nome or "").strip(), (detalhe or "").strip(), (ip or "").strip()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("Falha ao registrar evento de auditoria (%s).", evento)


def registrar_de_usuario(evento: str, user: Optional[Dict], *, detalhe: str = "", ip: str = "") -> None:
    """Atalho quando já se tem o dict do usuário (session/state)."""
    user = user or {}
    registrar(evento, usuario_id=user.get("id"), login=user.get("login", ""),
              nome=user.get("nome", ""), detalhe=detalhe, ip=ip)


def _where(evento: Optional[str], usuario_id: Optional[int], busca: Optional[str],
           desde: Optional[str], ate: Optional[str]) -> Tuple[str, List]:
    """Monta a cláusula WHERE compartilhada por listar()/resumo() — filtro único,
    sem divergência entre a página exibida e a contagem/KPIs."""
    where, params = [], []
    if evento:
        where.append("evento=?"); params.append(evento)
    if usuario_id is not None:
        where.append("usuario_id=?"); params.append(usuario_id)
    if desde:
        where.append("ts>=?"); params.append(desde)
    if ate:
        where.append("ts<=?"); params.append(ate + "T23:59:59")
    if busca:
        termo = f"%{busca.strip().lower()}%"
        where.append("(LOWER(login) LIKE ? OR LOWER(nome) LIKE ? OR LOWER(detalhe) LIKE ?)")
        params += [termo, termo, termo]
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def listar(limit: int = 100, evento: Optional[str] = None, usuario_id: Optional[int] = None,
           busca: Optional[str] = None, desde: Optional[str] = None,
           ate: Optional[str] = None, offset: int = 0) -> List[Dict]:
    """Uma página de eventos, mais recentes primeiro, com filtros opcionais.
    `desde`/`ate` no formato AAAA-MM-DD (inclusivo). `busca` casa login/nome/detalhe.
    LIMIT/OFFSET são resolvidos no SQL — não traz linha desnecessária."""
    clause, params = _where(evento, usuario_id, busca, desde, ate)
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    sql = "SELECT * FROM access_log" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?"
    conn = _get_conn()
    try:
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
        return [{
            "id": r["id"], "ts": r["ts"], "evento": r["evento"],
            "evento_label": EVENTOS.get(r["evento"], r["evento"]),
            "usuario_id": r["usuario_id"], "login": r["login"], "nome": r["nome"],
            "detalhe": r["detalhe"], "ip": r["ip"],
        } for r in rows]
    finally:
        conn.close()


def resumo(evento: Optional[str] = None, usuario_id: Optional[int] = None,
           busca: Optional[str] = None, desde: Optional[str] = None,
           ate: Optional[str] = None) -> Dict:
    """Total e KPIs por evento sobre TODO o conjunto filtrado (não só a página),
    numa varredura só (COUNT + GROUP BY). Base para o pager e os cartões."""
    clause, params = _where(evento, usuario_id, busca, desde, ate)
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM access_log" + clause, params).fetchone()[0]
        por_evento = {r["evento"]: r["n"] for r in conn.execute(
            "SELECT evento, COUNT(*) AS n FROM access_log" + clause + " GROUP BY evento", params)}
        return {"total": total, "por_evento": por_evento}
    finally:
        conn.close()


def usuarios_no_log() -> List[Dict]:
    """Usuários distintos que aparecem no log (para popular o filtro)."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT usuario_id, login, MAX(nome) AS nome FROM access_log "
            "WHERE usuario_id IS NOT NULL GROUP BY usuario_id, login ORDER BY login"
        ).fetchall()
        return [{"usuario_id": r["usuario_id"], "login": r["login"], "nome": r["nome"]} for r in rows]
    finally:
        conn.close()


def purgar(dias: int) -> int:
    """Remove eventos com mais de `dias` dias. Retorna quantos foram removidos.
    Chamado sob demanda (não automático) — retenção fica a critério do admin."""
    if dias <= 0:
        return 0
    limite = (datetime.now() - timedelta(days=dias)).isoformat(timespec="seconds")
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM access_log WHERE ts < ?", (limite,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
