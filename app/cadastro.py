"""
Cadastro mestre de transportadoras — a "base de acordo" do Freight Gateway.

É a fonte única do que é uma transportadora (nome/apelido/CNPJs/origem/metas) e
das suas tabelas de preço e prazo. Fica em SQLite próprio (`tables/cadastro.db`),
separado do histórico de auditoria, e SOBREPÕE as planilhas Excel: o motor de
frete lê do Excel e do banco, e o banco vence por transportadora (build_*_overlay).

Nada é migrado à força — cada transportadora só passa a vir do banco depois de
cadastrada/importada aqui. O vínculo CNPJ→transportadora é espelhado no JSON de
`app/carrier_mapping.py` (write-through), então o auditor continua intocado.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from app.carrier_mapping import SPOT_SENTINEL, load_mapping, save_mapping
from app.loaders import WEIGHT_BANDS

DB_PATH = os.getenv("CADASTRO_DB", str(Path(__file__).parent.parent / "tables" / "cadastro.db"))

# Chaves de faixa de peso aceitas (ordem = faixas fixas + excedentes), espelha loaders
BAND_KEYS: List[str] = [bk for bk, _ in WEIGHT_BANDS] + ["exc_150", "exc_200", "ac200", "ac1000", "ac4000"]

# Campos "meta" (encargos padrão do acordo) guardados na própria transportadora
META_FIELDS = ("gris_pct", "gris_min", "pedagio_ref", "tas", "despacho", "tec_pct", "cubagem_fator",
               "pos_pct", "pos_min", "trt_pct", "trt_min", "icms_embutido", "icms_aliq_fixa",
               "trt_so_hub")

# Defaults de metas de serviço (espelham auditor/scorecard)
DEFAULT_META_OTD = 95.0
DEFAULT_ATENCAO_OTD = 85.0
DEFAULT_TOL_ABS = 1.0
DEFAULT_TOL_PCT = 2.0


def _clean_cnpj(v: str) -> str:
    return "".join(c for c in (v or "") if c.isdigit())


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transportadoras (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            nome          TEXT NOT NULL UNIQUE,
            apelido       TEXT DEFAULT '',
            origem        TEXT DEFAULT '',
            ativo         INTEGER DEFAULT 1,
            cor           TEXT DEFAULT '#8bc34a',
            meta_otd      REAL DEFAULT 95,
            atencao_otd   REAL DEFAULT 85,
            tol_div_abs   REAL DEFAULT 1.0,
            tol_div_pct   REAL DEFAULT 2.0,
            gris_pct      REAL DEFAULT 0,
            gris_min      REAL DEFAULT 0,
            pedagio_ref   REAL DEFAULT 0,
            tas           REAL DEFAULT 0,
            despacho      REAL DEFAULT 0,
            tec_pct       REAL DEFAULT 0,
            cubagem_fator REAL DEFAULT 300,
            pos_pct       REAL DEFAULT 0,
            pos_min       REAL DEFAULT 0,
            trt_pct       REAL DEFAULT 0,
            trt_min       REAL DEFAULT 0,
            icms_embutido REAL DEFAULT 0,
            icms_aliq_fixa REAL DEFAULT 0,
            trt_so_hub    REAL DEFAULT 0,
            criado_em     TEXT,
            atualizado_em TEXT
        );

        CREATE TABLE IF NOT EXISTS transportadora_cnpjs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            transportadora_id INTEGER NOT NULL REFERENCES transportadoras(id) ON DELETE CASCADE,
            cnpj              TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS tabela_preco (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            transportadora_id INTEGER NOT NULL REFERENCES transportadoras(id) ON DELETE CASCADE,
            uf                TEXT DEFAULT '',
            regiao            TEXT DEFAULT '',
            descricao         TEXT DEFAULT '',
            faixas            TEXT DEFAULT '{}',
            advalorem_pct     REAL DEFAULT 0,
            frete_val_pct     REAL DEFAULT 0,
            sec_cat           REAL DEFAULT 0,
            pedagio_col       REAL DEFAULT 0,
            despacho          REAL
        );

        CREATE TABLE IF NOT EXISTS tabela_prazo (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            transportadora_id INTEGER NOT NULL REFERENCES transportadoras(id) ON DELETE CASCADE,
            uf                TEXT DEFAULT '',
            cidade            TEXT DEFAULT '',
            regiao            TEXT DEFAULT '',
            cep_inicial       TEXT,
            cep_final         TEXT,
            prazo_dias        INTEGER DEFAULT 0,
            tda               TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS ix_preco_transp ON tabela_preco(transportadora_id);
        CREATE INDEX IF NOT EXISTS ix_prazo_transp ON tabela_prazo(transportadora_id);
        """
    )
    # Migração: despacho por linha de preço (bancos criados antes desta coluna).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tabela_preco)").fetchall()}
    if "despacho" not in cols:
        conn.execute("ALTER TABLE tabela_preco ADD COLUMN despacho REAL")
    # Migração: POS (Participação Obrigatória do Segurado) na meta da transportadora.
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(transportadoras)").fetchall()}
    for c in ("pos_pct", "pos_min", "trt_pct", "trt_min", "icms_embutido", "icms_aliq_fixa", "trt_so_hub"):
        if c not in tcols:
            conn.execute(f"ALTER TABLE transportadoras ADD COLUMN {c} REAL DEFAULT 0")
    # Migração: vigência da tabela atual (AAAA-MM-DD). NULL = "sempre valeu" → o
    # auditor usa a tabela atual para qualquer data (comportamento anterior).
    if "vigencia_inicio" not in tcols:
        conn.execute("ALTER TABLE transportadoras ADD COLUMN vigencia_inicio TEXT")
    # Vigências arquivadas: snapshot completo (meta+preço+prazo) da tabela que valeu
    # num intervalo [inicio, fim]. O auditor escolhe a tabela pela data de emissão
    # do CTe, evitando divergência falsa ao auditar meses anteriores a um reajuste.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tabela_vigencias (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            transportadora_id INTEGER NOT NULL REFERENCES transportadoras(id) ON DELETE CASCADE,
            vigencia_inicio   TEXT NOT NULL,
            vigencia_fim      TEXT NOT NULL,
            descricao         TEXT DEFAULT '',
            criado_em         TEXT,
            dados             TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_vig_transp ON tabela_vigencias(transportadora_id)")
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ─── Mapeamento CNPJ (write-through para o carrier_mapping do auditor) ────────

def _apply_mapping(removidos: List[str], cnpjs: List[str], nome: str) -> None:
    """Espelha os CNPJs no JSON que o auditor lê. Remove os que saíram, aponta os
    atuais para `nome`. Preserva entradas SPOT e mapeamentos manuais de terceiros."""
    mapping = load_mapping()
    for c in removidos:
        cc = _clean_cnpj(c)
        # só remove se apontava para esta transportadora (não mexe em SPOT/terceiros)
        if cc in mapping and mapping[cc] != SPOT_SENTINEL:
            mapping.pop(cc, None)
    for c in cnpjs:
        cc = _clean_cnpj(c)
        if cc:
            mapping[cc] = nome
    save_mapping(mapping)


def _cnpjs_de(conn: sqlite3.Connection, transp_id: int) -> List[str]:
    rows = conn.execute(
        "SELECT cnpj FROM transportadora_cnpjs WHERE transportadora_id=? ORDER BY cnpj", (transp_id,)
    ).fetchall()
    return [r["cnpj"] for r in rows]


# ─── CRUD Transportadoras ────────────────────────────────────────────────────

def _row_to_transportadora(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict:
    tid = row["id"]
    n_preco = conn.execute(
        "SELECT COUNT(*) FROM tabela_preco WHERE transportadora_id=?", (tid,)
    ).fetchone()[0]
    n_prazo = conn.execute(
        "SELECT COUNT(*) FROM tabela_prazo WHERE transportadora_id=?", (tid,)
    ).fetchone()[0]
    d = dict(row)
    d["ativo"] = bool(d["ativo"])
    d["cnpjs"] = _cnpjs_de(conn, tid)
    d["linhas_preco"] = n_preco
    d["linhas_prazo"] = n_prazo
    return d


def list_transportadoras() -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM transportadoras ORDER BY nome").fetchall()
        return [_row_to_transportadora(conn, r) for r in rows]
    finally:
        conn.close()


def get_transportadora(transp_id: int) -> Optional[Dict]:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM transportadoras WHERE id=?", (transp_id,)).fetchone()
        return _row_to_transportadora(conn, row) if row else None
    finally:
        conn.close()


_CAMPOS_TRANSP = (
    "nome", "apelido", "origem", "ativo", "cor",
    "meta_otd", "atencao_otd", "tol_div_abs", "tol_div_pct",
) + META_FIELDS


def _valores_transp(data: Dict) -> Dict:
    """Filtra/normaliza o payload de entrada para as colunas conhecidas."""
    out: Dict = {}
    for campo in _CAMPOS_TRANSP:
        if campo in data and data[campo] is not None:
            out[campo] = data[campo]
    if "ativo" in out:
        out["ativo"] = 1 if out["ativo"] else 0
    if "nome" in out:
        out["nome"] = str(out["nome"]).strip()
    return out


def create_transportadora(data: Dict) -> Dict:
    vals = _valores_transp(data)
    if not vals.get("nome"):
        raise ValueError("Nome da transportadora é obrigatório.")
    cnpjs = [_clean_cnpj(c) for c in (data.get("cnpjs") or []) if _clean_cnpj(c)]
    conn = _get_conn()
    try:
        cols = list(vals.keys()) + ["criado_em", "atualizado_em"]
        placeholders = ", ".join("?" for _ in cols)
        agora = _now()
        conn.execute(
            f"INSERT INTO transportadoras ({', '.join(cols)}) VALUES ({placeholders})",
            list(vals.values()) + [agora, agora],
        )
        tid = conn.execute("SELECT id FROM transportadoras WHERE nome=?", (vals["nome"],)).fetchone()[0]
        for c in cnpjs:
            conn.execute(
                "INSERT OR IGNORE INTO transportadora_cnpjs (transportadora_id, cnpj) VALUES (?, ?)",
                (tid, c),
            )
        conn.commit()
    finally:
        conn.close()
    _apply_mapping([], cnpjs, vals["nome"])
    return get_transportadora(tid)


def update_transportadora(transp_id: int, data: Dict) -> Optional[Dict]:
    conn = _get_conn()
    try:
        atual = conn.execute("SELECT * FROM transportadoras WHERE id=?", (transp_id,)).fetchone()
        if atual is None:
            return None
        cnpjs_antigos = _cnpjs_de(conn, transp_id)
        vals = _valores_transp(data)
        vals["atualizado_em"] = _now()
        set_clause = ", ".join(f"{k}=?" for k in vals)
        conn.execute(
            f"UPDATE transportadoras SET {set_clause} WHERE id=?",
            list(vals.values()) + [transp_id],
        )
        cnpjs_novos: Optional[List[str]] = None
        if "cnpjs" in data:
            cnpjs_novos = [_clean_cnpj(c) for c in (data.get("cnpjs") or []) if _clean_cnpj(c)]
            conn.execute("DELETE FROM transportadora_cnpjs WHERE transportadora_id=?", (transp_id,))
            for c in cnpjs_novos:
                conn.execute(
                    "INSERT OR IGNORE INTO transportadora_cnpjs (transportadora_id, cnpj) VALUES (?, ?)",
                    (transp_id, c),
                )
        nome = vals.get("nome") or atual["nome"]
        conn.commit()
    finally:
        conn.close()
    if cnpjs_novos is not None:
        _apply_mapping(cnpjs_antigos, cnpjs_novos, nome)
    elif vals.get("nome") and vals["nome"] != atual["nome"]:
        # nome mudou mas cnpjs não vieram no payload: re-aponta os existentes
        _apply_mapping(cnpjs_antigos, cnpjs_antigos, nome)
    return get_transportadora(transp_id)


def delete_transportadora(transp_id: int) -> bool:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT nome FROM transportadoras WHERE id=?", (transp_id,)).fetchone()
        if row is None:
            return False
        cnpjs = _cnpjs_de(conn, transp_id)
        conn.execute("DELETE FROM transportadoras WHERE id=?", (transp_id,))
        conn.commit()
    finally:
        conn.close()
    _apply_mapping(cnpjs, [], row["nome"])
    return True


# ─── CRUD Tabela de Preço ────────────────────────────────────────────────────

def _norm_faixas(faixas) -> Dict[str, float]:
    if isinstance(faixas, str):
        try:
            faixas = json.loads(faixas)
        except (json.JSONDecodeError, TypeError):
            faixas = {}
    out: Dict[str, float] = {}
    for k, v in (faixas or {}).items():
        if k in BAND_KEYS and v not in (None, "", "nan"):
            try:
                out[k] = float(v)
            except (ValueError, TypeError):
                pass
    return out


def _row_to_preco(row: sqlite3.Row) -> Dict:
    d = dict(row)
    d["faixas"] = _norm_faixas(d.get("faixas"))
    return d


def list_precos(transp_id: int) -> List[Dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tabela_preco WHERE transportadora_id=? ORDER BY uf, regiao, id", (transp_id,)
        ).fetchall()
        return [_row_to_preco(r) for r in rows]
    finally:
        conn.close()


def save_precos(transp_id: int, linhas: List[Dict]) -> List[Dict]:
    """Substitui todas as linhas de preço da transportadora (salvar em lote)."""
    conn = _get_conn()
    try:
        if conn.execute("SELECT 1 FROM transportadoras WHERE id=?", (transp_id,)).fetchone() is None:
            raise ValueError("Transportadora não encontrada.")
        conn.execute("DELETE FROM tabela_preco WHERE transportadora_id=?", (transp_id,))
        for ln in linhas:
            desp = ln.get("despacho")
            desp = float(desp) if desp not in (None, "") else None
            conn.execute(
                """INSERT INTO tabela_preco
                   (transportadora_id, uf, regiao, descricao, faixas,
                    advalorem_pct, frete_val_pct, sec_cat, pedagio_col, despacho)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    transp_id,
                    str(ln.get("uf", "")).strip().upper(),
                    str(ln.get("regiao", "")).strip(),
                    str(ln.get("descricao", "")).strip(),
                    json.dumps(_norm_faixas(ln.get("faixas"))),
                    float(ln.get("advalorem_pct") or 0),
                    float(ln.get("frete_val_pct") or 0),
                    float(ln.get("sec_cat") or 0),
                    float(ln.get("pedagio_col") or 0),
                    desp,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return list_precos(transp_id)


# ─── CRUD Tabela de Prazo ────────────────────────────────────────────────────

def _norm_cep(v) -> Optional[str]:
    s = "".join(c for c in str(v or "") if c.isdigit())
    return s.zfill(8) if s else None


def _limpa(v) -> str:
    """Texto seguro para gravar: trata NaN/None/'nan' como vazio."""
    if v is None or (isinstance(v, float) and not pd.notna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def list_prazos(transp_id: int, search: Optional[str] = None,
                limit: int = 500, offset: int = 0) -> Dict:
    conn = _get_conn()
    try:
        where = "transportadora_id=?"
        params: List = [transp_id]
        if search:
            where += " AND (UPPER(cidade) LIKE ? OR UPPER(uf) LIKE ? OR UPPER(regiao) LIKE ?)"
            like = f"%{search.upper()}%"
            params += [like, like, like]
        total = conn.execute(f"SELECT COUNT(*) FROM tabela_prazo WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM tabela_prazo WHERE {where} ORDER BY uf, cidade, id LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return {"total": total, "linhas": [dict(r) for r in rows]}
    finally:
        conn.close()


def save_prazos(transp_id: int, linhas: List[Dict], substituir: bool = True) -> Dict:
    """Grava linhas de prazo. substituir=True troca tudo; False acrescenta (usado no import)."""
    conn = _get_conn()
    try:
        if conn.execute("SELECT 1 FROM transportadoras WHERE id=?", (transp_id,)).fetchone() is None:
            raise ValueError("Transportadora não encontrada.")
        if substituir:
            conn.execute("DELETE FROM tabela_prazo WHERE transportadora_id=?", (transp_id,))
        for ln in linhas:
            conn.execute(
                """INSERT INTO tabela_prazo
                   (transportadora_id, uf, cidade, regiao, cep_inicial, cep_final, prazo_dias, tda)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    transp_id,
                    str(ln.get("uf", "")).strip().upper(),
                    str(ln.get("cidade", "")).strip().upper(),
                    str(ln.get("regiao", "")).strip(),
                    _norm_cep(ln.get("cep_inicial")),
                    _norm_cep(ln.get("cep_final")),
                    int(float(ln.get("prazo_dias") or 0)),
                    str(ln.get("tda", "")).strip(),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return list_prazos(transp_id)


def upsert_prazos(transp_id: int, linhas: List[Dict]) -> Dict:
    """Atualiza/insere linhas de prazo SEM apagar as demais (seguro com paginação).

    Linha com `id` existente é atualizada; sem `id` (ou id inexistente) é inserida.
    Nunca faz DELETE — a remoção é feita linha a linha pelo botão ✕ (delete_linha).
    """
    conn = _get_conn()
    try:
        if conn.execute("SELECT 1 FROM transportadoras WHERE id=?", (transp_id,)).fetchone() is None:
            raise ValueError("Transportadora não encontrada.")
        for ln in linhas:
            vals = (
                str(ln.get("uf", "")).strip().upper(),
                str(ln.get("cidade", "")).strip().upper(),
                str(ln.get("regiao", "")).strip(),
                _norm_cep(ln.get("cep_inicial")),
                _norm_cep(ln.get("cep_final")),
                int(float(ln.get("prazo_dias") or 0)),
                str(ln.get("tda", "")).strip(),
            )
            rid = ln.get("id")
            done = False
            if rid:
                cur = conn.execute(
                    """UPDATE tabela_prazo
                       SET uf=?, cidade=?, regiao=?, cep_inicial=?, cep_final=?, prazo_dias=?, tda=?
                       WHERE id=? AND transportadora_id=?""",
                    (*vals, rid, transp_id),
                )
                done = cur.rowcount > 0
            if not done:
                conn.execute(
                    """INSERT INTO tabela_prazo
                       (transportadora_id, uf, cidade, regiao, cep_inicial, cep_final, prazo_dias, tda)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (transp_id, *vals),
                )
        conn.commit()
    finally:
        conn.close()
    return list_prazos(transp_id)


def delete_linha(tabela: str, transp_id: int, rid: int) -> bool:
    assert tabela in ("tabela_preco", "tabela_prazo")
    conn = _get_conn()
    try:
        cur = conn.execute(
            f"DELETE FROM {tabela} WHERE id=? AND transportadora_id=?", (rid, transp_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ─── Import a partir das planilhas Excel já carregadas ───────────────────────

def seed_from_excel(transp_id: int, precos_excel: Dict, prazos_excel: Dict) -> Dict:
    """Copia as linhas da aba Excel de mesmo nome da transportadora para o banco,
    para que passem a ser editáveis. Retorna o que foi importado."""
    transp = get_transportadora(transp_id)
    if transp is None:
        raise ValueError("Transportadora não encontrada.")
    nome = transp["nome"]
    resultado = {"precos": 0, "prazos": 0, "meta_atualizada": False}

    pdata = precos_excel.get(nome)
    if pdata is not None:
        df = pdata["df"]
        band_keys = pdata.get("band_keys", [])
        linhas = []
        for _, r in df.iterrows():
            uf = str(r.get("UF", "")).strip().upper()
            if len(uf) != 2 or not uf.isalpha():
                continue
            faixas = {}
            for bk in band_keys:
                v = r.get(bk)
                if pd.notna(v) and float(v or 0) > 0:
                    faixas[bk] = float(v)
            desc = r.get("DESCRICAO")
            if desc is None or not pd.notna(desc):
                desc = r.get("descricao")
            linhas.append({
                "uf": uf,
                "regiao": _limpa(r.get("REGIAO")),
                "descricao": _limpa(desc),
                "faixas": faixas,
                "advalorem_pct": float(r.get("advalorem_pct", 0) or 0),
                "frete_val_pct": float(r.get("frete_val_pct", 0) or 0),
                "sec_cat": float(r.get("sec_cat", 0) or 0),
                "pedagio_col": float(r.get("pedagio_col", 0) or 0),
            })
        save_precos(transp_id, linhas)
        resultado["precos"] = len(linhas)
        # meta (encargos padrão) da aba → transportadora
        meta = pdata.get("meta", {})
        upd = {k: meta[k] for k in META_FIELDS if k in meta}
        if pdata.get("origin") and not transp.get("origem"):
            upd["origem"] = pdata["origin"]
        if upd:
            update_transportadora(transp_id, upd)
            resultado["meta_atualizada"] = True

    pz = prazos_excel.get(nome)
    if pz is not None:
        linhas = []
        for _, r in pz.iterrows():
            linhas.append({
                "uf": _limpa(r.get("UF")).upper(),
                "cidade": _limpa(r.get("CIDADE")),
                "regiao": _limpa(r.get("REGIAO")),
                "cep_inicial": r.get("CEP_INICIAL"),
                "cep_final": r.get("CEP_FINAL"),
                "prazo_dias": int(r.get("PRAZO_DIAS", 0) or 0),
            })
        save_prazos(transp_id, linhas)
        resultado["prazos"] = len(linhas)

    return resultado


def _cnpjs_por_transportadora() -> Dict[str, List[str]]:
    """Reverte o mapeamento CNPJ→transportadora para transportadora→[CNPJs],
    ignorando SPOT, para popular a base de acordo com os CNPJs já conhecidos."""
    rev: Dict[str, List[str]] = {}
    for cnpj, carrier in load_mapping().items():
        if carrier and carrier != SPOT_SENTINEL:
            rev.setdefault(carrier, []).append(cnpj)
    return rev


def _transp_por_nome(nome: str) -> Optional[Dict]:
    for t in list_transportadoras():
        if t["nome"].strip().lower() == nome.strip().lower():
            return t
    return None


def importar_planilha(dados: Dict) -> Dict:
    """Upsert de uma transportadora a partir do dict lido de app.cadastro_planilha.
    Cria/atualiza o cadastro e SUBSTITUI as linhas de preço e prazo. Retorna resumo."""
    transp = dict(dados.get("transportadora") or {})
    nome = (transp.get("nome") or "").strip()
    if not nome:
        raise ValueError("A aba Transportadora precisa ter o campo Nome preenchido.")
    # limpar chaves nulas para não sobrescrever com None
    transp = {k: v for k, v in transp.items() if v is not None}

    existente = _transp_por_nome(nome)
    if existente:
        transp["nome"] = existente["nome"]  # preserva o nome canônico
        t = update_transportadora(existente["id"], transp)
        acao = "atualizada"
    else:
        t = create_transportadora(transp)
        acao = "criada"

    tid = t["id"]
    precos = dados.get("precos") or []
    prazos = dados.get("prazos") or []
    if precos:
        save_precos(tid, precos)
    if prazos:
        save_prazos(tid, prazos)
    return {"transportadora": t["nome"], "id": tid, "acao": acao,
            "precos": len(precos), "prazos": len(prazos)}


def seed_all_from_excel(precos_excel: Dict, prazos_excel: Dict) -> List[str]:
    """Importa para o banco toda transportadora que exista nas planilhas Excel e
    ainda não esteja cadastrada. Puxa os CNPJs do mapeamento existente. Usado no
    primeiro boot (base vazia) e pelo botão 'Importar todas do Excel'."""
    rev = _cnpjs_por_transportadora()
    existentes = {t["nome"] for t in list_transportadoras()}
    nomes = sorted(set(precos_excel) | set(prazos_excel))
    criadas: List[str] = []
    for nome in nomes:
        if nome in existentes:
            continue
        t = create_transportadora({"nome": nome, "cnpjs": rev.get(nome, [])})
        seed_from_excel(t["id"], precos_excel, prazos_excel)
        criadas.append(nome)
    return criadas


# ─── Overlay para o motor de frete ───────────────────────────────────────────

def _precos_data_from_rows(nome: str, origem: str, meta: Dict, preco_rows: List[Dict]) -> Dict:
    """Monta o shape de precos_data (o que o rate_calculator consome) a partir de
    linhas de preço já em dict — usado tanto pela tabela atual quanto por um snapshot
    de vigência, garantindo forma idêntica."""
    registros = []
    band_keys: set = set()
    for r in preco_rows:
        faixas = _norm_faixas(r.get("faixas"))
        band_keys.update(faixas.keys())
        despacho = r.get("despacho")
        reg = {
            "UF": str(r.get("uf", "") or "").strip().upper(),
            "REGIAO": str(r.get("regiao", "") or "").strip(),
            "advalorem_pct": float(r.get("advalorem_pct") or 0),
            "frete_val_pct": float(r.get("frete_val_pct") or 0),
            "sec_cat": float(r.get("sec_cat") or 0),
            "pedagio_col": float(r.get("pedagio_col") or 0),
            "despacho": (float(despacho) if despacho not in (None, "") else None),
        }
        reg.update(faixas)
        registros.append(reg)
    df = pd.DataFrame(registros)
    for bk in band_keys:
        if bk not in df.columns:
            df[bk] = np.nan
        df[bk] = pd.to_numeric(df[bk], errors="coerce")
    return {
        "carrier": nome,
        "origin": origem or "",
        "meta": {k: float(meta.get(k) or 0) for k in META_FIELDS},
        "band_keys": sorted(band_keys),
        "df": df,
    }


def _prazos_df_from_rows(prazo_rows: List[Dict]) -> pd.DataFrame:
    """DataFrame de prazos no shape que o rate_calculator consome, a partir de dicts."""
    df = pd.DataFrame(
        [
            {
                "UF": str(r.get("uf", "") or "").strip().upper(),
                "CIDADE": str(r.get("cidade", "") or "").strip().upper(),
                "PRAZO_DIAS": int(r.get("prazo_dias") or 0),
                "REGIAO": str(r.get("regiao", "") or "").strip(),
                "CEP_INICIAL": r.get("cep_inicial"),
                "CEP_FINAL": r.get("cep_final"),
            }
            for r in prazo_rows
        ]
    )
    if df.empty:
        return df
    df["has_cep"] = df["CEP_INICIAL"].notna() & df["CEP_FINAL"].notna()
    return df.reset_index(drop=True)


def build_precos_overlay() -> Dict[str, Dict]:
    """Monta o mesmo shape de app.loaders.load_precos a partir do banco.
    Só inclui transportadoras ativas que tenham ao menos uma linha de preço."""
    conn = _get_conn()
    try:
        transps = conn.execute("SELECT * FROM transportadoras WHERE ativo=1").fetchall()
        overlay: Dict[str, Dict] = {}
        for t in transps:
            rows = conn.execute(
                "SELECT * FROM tabela_preco WHERE transportadora_id=?", (t["id"],)
            ).fetchall()
            if not rows:
                continue
            overlay[t["nome"]] = _precos_data_from_rows(
                t["nome"], t["origem"] or "", dict(t), [dict(r) for r in rows])
        return overlay
    finally:
        conn.close()


def build_prazos_overlay() -> Dict[str, pd.DataFrame]:
    """Monta o mesmo shape de app.loaders.load_prazos a partir do banco."""
    conn = _get_conn()
    try:
        transps = conn.execute("SELECT id, nome FROM transportadoras WHERE ativo=1").fetchall()
        overlay: Dict[str, pd.DataFrame] = {}
        for t in transps:
            rows = conn.execute(
                "SELECT uf, cidade, regiao, cep_inicial, cep_final, prazo_dias "
                "FROM tabela_prazo WHERE transportadora_id=?", (t["id"],)
            ).fetchall()
            if not rows:
                continue
            overlay[t["nome"]] = _prazos_df_from_rows([dict(r) for r in rows])
        return overlay
    finally:
        conn.close()


# ─── Vigência de tabela (versionamento por data) ─────────────────────────────
_VIGENCIA_PISO = "2000-01-01"  # data-piso quando a tabela atual nunca teve vigência


def _serializar_tabela(conn, transp_id: int) -> Dict:
    """Congela a tabela atual (meta + preços + prazos) num dict serializável."""
    t = conn.execute("SELECT * FROM transportadoras WHERE id=?", (transp_id,)).fetchone()
    precos = conn.execute(
        "SELECT uf, regiao, descricao, faixas, advalorem_pct, frete_val_pct, "
        "sec_cat, pedagio_col, despacho FROM tabela_preco WHERE transportadora_id=?",
        (transp_id,)).fetchall()
    prazos = conn.execute(
        "SELECT uf, cidade, regiao, cep_inicial, cep_final, prazo_dias "
        "FROM tabela_prazo WHERE transportadora_id=?", (transp_id,)).fetchall()
    return {
        "origem": t["origem"] or "",
        "meta": {k: (t[k] if k in t.keys() else 0) for k in META_FIELDS},
        "precos": [dict(r) for r in precos],
        "prazos": [dict(r) for r in prazos],
    }


def arquivar_e_reajustar(transp_id: int, nova_vigencia_inicio: str, descricao: str = "") -> Dict:
    """Registra um reajuste: arquiva a tabela ATUAL como uma vigência fechada até a
    véspera de `nova_vigencia_inicio` e marca a tabela atual como válida a partir
    dessa data. O usuário edita os valores novos depois (a tabela viva continua a
    mesma até ele alterar). `nova_vigencia_inicio` no formato AAAA-MM-DD."""
    from datetime import date, timedelta
    try:
        d = date.fromisoformat(nova_vigencia_inicio[:10])
    except ValueError as e:
        raise ValueError("Data de vigência inválida (use AAAA-MM-DD).") from e
    fim = (d - timedelta(days=1)).isoformat()
    conn = _get_conn()
    try:
        t = conn.execute("SELECT vigencia_inicio FROM transportadoras WHERE id=?", (transp_id,)).fetchone()
        if t is None:
            raise ValueError("Transportadora não encontrada.")
        inicio_atual = t["vigencia_inicio"] or _VIGENCIA_PISO
        if inicio_atual >= nova_vigencia_inicio:
            raise ValueError("A nova vigência precisa ser posterior ao início da vigência atual.")
        dados = _serializar_tabela(conn, transp_id)
        conn.execute(
            "INSERT INTO tabela_vigencias (transportadora_id, vigencia_inicio, vigencia_fim, "
            "descricao, criado_em, dados) VALUES (?,?,?,?,?,?)",
            (transp_id, inicio_atual, fim, descricao or "", _now(),
             json.dumps(dados, ensure_ascii=False)))
        conn.execute("UPDATE transportadoras SET vigencia_inicio=?, atualizado_em=? WHERE id=?",
                     (nova_vigencia_inicio, _now(), transp_id))
        conn.commit()
        return {"arquivada_ate": fim, "nova_vigencia_inicio": nova_vigencia_inicio}
    finally:
        conn.close()


def list_vigencias(transp_id: int) -> Dict:
    """Vigência atual + histórico de vigências arquivadas (sem o payload `dados`)."""
    conn = _get_conn()
    try:
        t = conn.execute("SELECT vigencia_inicio FROM transportadoras WHERE id=?", (transp_id,)).fetchone()
        arquivadas = conn.execute(
            "SELECT id, vigencia_inicio, vigencia_fim, descricao, criado_em "
            "FROM tabela_vigencias WHERE transportadora_id=? ORDER BY vigencia_inicio DESC",
            (transp_id,)).fetchall()
        return {
            "vigencia_atual_inicio": (t["vigencia_inicio"] if t else None),
            "arquivadas": [dict(r) for r in arquivadas],
        }
    finally:
        conn.close()


def tabela_vigente_em(transp_id: int, data: str):
    """(precos_data, prazos_df) da tabela que valia em `data` (AAAA-MM-DD).
    Retorna None quando a tabela ATUAL se aplica (data >= vigência atual, ou sem
    vigência configurada) — aí o auditor usa o overlay global, sem custo extra."""
    data = (data or "")[:10]
    conn = _get_conn()
    try:
        t = conn.execute("SELECT nome, vigencia_inicio FROM transportadoras WHERE id=?",
                         (transp_id,)).fetchone()
        if t is None:
            return None
        inicio_atual = t["vigencia_inicio"]
        if not inicio_atual or not data or data >= inicio_atual:
            return None  # tabela atual se aplica
        snap = conn.execute(
            "SELECT dados FROM tabela_vigencias WHERE transportadora_id=? "
            "AND vigencia_inicio<=? AND vigencia_fim>=? ORDER BY vigencia_inicio DESC LIMIT 1",
            (transp_id, data, data)).fetchone()
        if snap is None:
            return None  # sem snapshot cobrindo a data → cai na tabela atual (seguro)
        dados = json.loads(snap["dados"])
        precos_data = _precos_data_from_rows(t["nome"], dados.get("origem", ""),
                                             dados.get("meta", {}), dados.get("precos", []))
        prazos_df = _prazos_df_from_rows(dados.get("prazos", []))
        return precos_data, prazos_df
    finally:
        conn.close()


def tabela_vigente_em_por_nome(nome: str, data: str):
    """Como tabela_vigente_em, mas indexado pelo NOME da transportadora (o que o
    auditor tem em mãos). Retorna None quando a tabela atual se aplica."""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT id FROM transportadoras WHERE nome=?", (nome,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return tabela_vigente_em(row["id"], data)


# ─── Metas por transportadora (consumidas por auditor e scorecard) ───────────

def tolerancia_por_transportadora(nome: str) -> Optional[Dict[str, float]]:
    """Retorna {'tol_abs','tol_pct'} da transportadora cadastrada, ou None."""
    if not nome:
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT tol_div_abs, tol_div_pct FROM transportadoras WHERE nome=?", (nome,)
        ).fetchone()
        if row is None:
            return None
        return {"tol_abs": float(row["tol_div_abs"]), "tol_pct": float(row["tol_div_pct"])}
    finally:
        conn.close()


def metas_otd() -> Dict[str, Dict[str, float]]:
    """Mapa nome -> {'meta','atencao'} para colorir o scorecard por transportadora."""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT nome, meta_otd, atencao_otd FROM transportadoras").fetchall()
        return {
            r["nome"]: {"meta": float(r["meta_otd"] or DEFAULT_META_OTD),
                        "atencao": float(r["atencao_otd"] or DEFAULT_ATENCAO_OTD)}
            for r in rows
        }
    finally:
        conn.close()
