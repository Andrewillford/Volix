"""
Taxas condicionais de entrega (TDA / TRT / EMEX / TDE) por transportadora.

Alguns encargos só incidem quando o DESTINO (CEP) ou o DESTINATÁRIO (CNPJ) está
numa lista da transportadora:
  • TDA  — Taxa de Dificuldade de Acesso: valor R$ FIXO por cidade/CEP.
  • TRT  — Taxa de Restrição de Trânsito: % sobre o frete (mín), se o CEP está na lista.
  • EMEX — Emergência Excepcional/Área de Risco: % sobre o valor da NF (mín), por CEP.
  • TDE  — Taxa de Dificuldade de Entrega: % sobre o frete, por CNPJ do destinatário.

Guardado em SQLite próprio (tables/taxas.db) — as listas são grandes (TDE ~167k).
As alíquotas/mínimos (TRT/EMEX/TDE) ficam em `taxa_config` por transportadora.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, Optional

DB_PATH = os.getenv("TAXAS_DB", str(Path(__file__).parent.parent / "tables" / "taxas.db"))


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS taxa_cep (
            transportadora TEXT NOT NULL,
            tipo    TEXT NOT NULL,          -- TDA | TRT | EMEX
            cep_ini TEXT NOT NULL,
            cep_fin TEXT NOT NULL,
            valor   REAL DEFAULT 0          -- só a TDA usa valor fixo
        );
        CREATE INDEX IF NOT EXISTS ix_taxacep ON taxa_cep(transportadora, tipo, cep_ini, cep_fin);

        CREATE TABLE IF NOT EXISTS taxa_dest (
            transportadora TEXT NOT NULL,
            cnpj TEXT NOT NULL,             -- só dígitos
            grau TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ix_taxadest ON taxa_dest(transportadora, cnpj);

        CREATE TABLE IF NOT EXISTS taxa_config (
            transportadora TEXT PRIMARY KEY,
            trt_pct REAL DEFAULT 0, trt_min REAL DEFAULT 0,
            emex_pct REAL DEFAULT 0, emex_min REAL DEFAULT 0,
            tde_pct REAL DEFAULT 0, tde_min REAL DEFAULT 0
        );
        """
    )
    _ensure_columns(c)
    c.commit()
    c.close()


# UFs da região Norte — usadas para a alíquota diferenciada de TRT (algumas
# transportadoras cobram mais nessa região; ex. Fitlog: 12%/R$38 vs 10%/R$14).
UFS_NORTE = {"AC", "AM", "AP", "PA", "RO", "RR", "TO"}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Migração leve (mesmo padrão de app.auth/app.history): adiciona a alíquota
    diferenciada de TRT para a região Norte em bancos já existentes."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(taxa_config)").fetchall()}
    if "trt_pct_norte" not in cols:
        conn.execute("ALTER TABLE taxa_config ADD COLUMN trt_pct_norte REAL")
    if "trt_min_norte" not in cols:
        conn.execute("ALTER TABLE taxa_config ADD COLUMN trt_min_norte REAL")
    if "emex_base" not in cols:
        conn.execute("ALTER TABLE taxa_config ADD COLUMN emex_base TEXT")


def _dig(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _cep8(v) -> Optional[str]:
    s = _dig(v)
    return s.zfill(8) if s else None


def set_config(transportadora: str, **kw) -> None:
    """trt_pct_norte/trt_min_norte são opcionais — se não informados (None/ausente),
    PRESERVAM o valor já salvo em vez de zerar, já que a maioria das transportadoras
    não tem alíquota diferenciada para o Norte e não deveria precisar repassá-la
    toda vez que ajustar outro campo."""
    init_db()
    c = _conn()
    campos_obrigatorios = ("trt_pct", "trt_min", "emex_pct", "emex_min", "tde_pct", "tde_min")
    campos_norte = ("trt_pct_norte", "trt_min_norte")
    vals = {k: float(kw.get(k, 0) or 0) for k in campos_obrigatorios}

    existente = c.execute(
        "SELECT trt_pct_norte, trt_min_norte, emex_base FROM taxa_config WHERE transportadora=?", (transportadora,)
    ).fetchone()
    for k in campos_norte:
        if kw.get(k) is not None:
            vals[k] = float(kw[k])
        else:
            vals[k] = existente[k] if existente else None
    # emex_base (str, opcional): preserva se não informado
    vals["emex_base"] = kw["emex_base"] if kw.get("emex_base") is not None else (existente["emex_base"] if existente else None)

    campos = campos_obrigatorios + campos_norte + ("emex_base",)
    placeholders = ",".join(["?"] * (len(campos) + 1))
    c.execute(
        f"INSERT INTO taxa_config (transportadora, {', '.join(campos)}) VALUES ({placeholders}) "
        f"ON CONFLICT(transportadora) DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in campos)}",
        [transportadora] + [vals[k] for k in campos],
    )
    c.commit()
    c.close()


def limpar(transportadora: str) -> None:
    init_db()
    c = _conn()
    for t in ("taxa_cep", "taxa_dest"):
        c.execute(f"DELETE FROM {t} WHERE transportadora=?", (transportadora,))
    c.commit()
    c.close()


def resumo(transportadora: str) -> Dict:
    init_db()
    c = _conn()
    out = {"tda": 0, "trt": 0, "emex": 0, "tde": 0}
    for tipo in ("TDA", "TRT", "EMEX"):
        out[tipo.lower()] = c.execute(
            "SELECT COUNT(*) FROM taxa_cep WHERE transportadora=? AND tipo=?", (transportadora, tipo)
        ).fetchone()[0]
    out["tde"] = c.execute("SELECT COUNT(*) FROM taxa_dest WHERE transportadora=?", (transportadora,)).fetchone()[0]
    cfg = c.execute("SELECT * FROM taxa_config WHERE transportadora=?", (transportadora,)).fetchone()
    out["config"] = dict(cfg) if cfg else None
    c.close()
    return out


# ─── Import das listas (formato Fitlog) ──────────────────────────────────────

def importar_listas(transportadora: str, arquivo_tda_trt_emex: str, arquivo_tde: Optional[str] = None) -> Dict:
    """Importa as listas da transportadora: TDA/TRT/EMEX (um workbook com 3 abas)
    e, opcionalmente, TDE (destinatários por CNPJ)."""
    import pandas as pd
    init_db()
    limpar(transportadora)
    c = _conn()
    res = {"tda": 0, "trt": 0, "emex": 0, "tde": 0}

    def _ranges(df, ci, cf, val_col=None):
        rows = []
        for _, r in df.iterrows():
            a, b = _cep8(r.get(ci)), _cep8(r.get(cf))
            if not a or not b:
                continue
            v = 0.0
            if val_col:
                try:
                    v = float(str(r.get(val_col)).replace(",", "."))
                except (ValueError, TypeError):
                    v = 0.0
            rows.append((a, b, v))
        return rows

    xls = pd.ExcelFile(arquivo_tda_trt_emex)
    for aba in xls.sheet_names:
        an = aba.strip().upper()
        raw = pd.read_excel(xls, sheet_name=aba, header=None, dtype=str)
        # localizar a linha de cabeçalho (contém CEP_INI / CEP INICIAL)
        hdr = None
        for i in range(min(8, len(raw))):
            joined = " ".join(str(v).upper() for v in raw.iloc[i].values if v)
            if "CEP" in joined and ("INI" in joined or "INICIAL" in joined):
                hdr = i
                break
        if hdr is None:
            continue
        df = pd.read_excel(xls, sheet_name=aba, header=hdr, dtype=str)
        df.columns = [str(x).strip().upper() for x in df.columns]
        ci = next((x for x in df.columns if "CEP" in x and ("INI" in x)), None)
        cf = next((x for x in df.columns if "CEP" in x and ("FIN" in x or "FINAL" in x)), None)
        if not ci or not cf:
            continue
        if an.startswith("TDA"):
            valc = next((x for x in df.columns if x == "TDA"), None)
            rows = _ranges(df, ci, cf, valc)
            c.executemany("INSERT INTO taxa_cep (transportadora,tipo,cep_ini,cep_fin,valor) VALUES (?,'TDA',?,?,?)",
                          [(transportadora, a, b, v) for a, b, v in rows])
            res["tda"] = len(rows)
        elif an.startswith("TRT"):
            rows = _ranges(df, ci, cf)
            c.executemany("INSERT INTO taxa_cep (transportadora,tipo,cep_ini,cep_fin,valor) VALUES (?,'TRT',?,?,0)",
                          [(transportadora, a, b) for a, b, _ in rows])
            res["trt"] = len(rows)
        elif an.startswith("EMEX"):
            rows = _ranges(df, ci, cf)
            c.executemany("INSERT INTO taxa_cep (transportadora,tipo,cep_ini,cep_fin,valor) VALUES (?,'EMEX',?,?,0)",
                          [(transportadora, a, b) for a, b, _ in rows])
            res["emex"] = len(rows)

    if arquivo_tde:
        xls2 = pd.ExcelFile(arquivo_tde)
        aba = xls2.sheet_names[0]
        raw = pd.read_excel(xls2, sheet_name=aba, header=None, dtype=str)
        hdr = next((i for i in range(min(8, len(raw)))
                    if any("CNPJ" in str(v).upper() for v in raw.iloc[i].values if v)), None)
        if hdr is not None:
            df = pd.read_excel(xls2, sheet_name=aba, header=hdr, dtype=str)
            df.columns = [str(x).strip().upper() for x in df.columns]
            col_cnpj = next((x for x in df.columns if "CNPJ" in x), None)
            col_grau = next((x for x in df.columns if "GRAU" in x), None)
            rows = []
            for _, r in df.iterrows():
                cc = _dig(r.get(col_cnpj))
                if cc:
                    rows.append((transportadora, cc, str(r.get(col_grau) or "").strip() if col_grau else ""))
            c.executemany("INSERT INTO taxa_dest (transportadora,cnpj,grau) VALUES (?,?,?)", rows)
            res["tde"] = len(rows)

    c.commit()
    c.close()
    return res


# ─── Cálculo das taxas para um destino/destinatário ──────────────────────────

def calcular(transportadora: str, dest_cep: str, dest_uf: str,
             cnpj_destinatario: str, frete_base: float, valor_nf: float) -> Dict[str, float]:
    """Retorna {tda,trt,emex,tde} aplicáveis (só as que baterem). frete_base =
    subtotal do frete (para os % de TRT/TDE); valor_nf para EMEX."""
    cep = _cep8(dest_cep)
    if not cep and not cnpj_destinatario:
        return {}
    c = _conn()
    try:
        cfg = c.execute("SELECT * FROM taxa_config WHERE transportadora=?", (transportadora,)).fetchone()
        cfg = dict(cfg) if cfg else {}
        out: Dict[str, float] = {}

        if cep:
            row = c.execute(
                "SELECT valor FROM taxa_cep WHERE transportadora=? AND tipo='TDA' "
                "AND cep_ini<=? AND cep_fin>=? ORDER BY valor DESC LIMIT 1",
                (transportadora, cep, cep),
            ).fetchone()
            if row and row["valor"]:
                out["tda"] = round(float(row["valor"]), 2)

            # POS condicional por CEP (ex.: TG cobra R$6/CTe só no estado do RJ) —
            # valor fixo por faixa, somado ao POS de meta (que é por transportadora).
            posr = c.execute(
                "SELECT valor FROM taxa_cep WHERE transportadora=? AND tipo='POS' "
                "AND cep_ini<=? AND cep_fin>=? ORDER BY valor DESC LIMIT 1",
                (transportadora, cep, cep),
            ).fetchone()
            if posr and posr["valor"]:
                out["pos"] = round(float(posr["valor"]), 2)

            trt = c.execute(
                "SELECT 1 FROM taxa_cep WHERE transportadora=? AND tipo='TRT' AND cep_ini<=? AND cep_fin>=? LIMIT 1",
                (transportadora, cep, cep),
            ).fetchone()
            if trt and (cfg.get("trt_pct") or cfg.get("trt_min")):
                # Alíquota diferenciada para a região Norte (ex.: Fitlog 12%/R$38
                # vs 10%/R$14 nas demais regiões) — usa a padrão se não configurada.
                usar_norte = (dest_uf or "").strip().upper() in UFS_NORTE and cfg.get("trt_pct_norte")
                pct = cfg["trt_pct_norte"] if usar_norte else cfg["trt_pct"]
                minimo = (cfg.get("trt_min_norte") or 0) if usar_norte else cfg.get("trt_min", 0)
                # Quando o mesmo CEP também tem TDA, o percentual do TRT incide
                # sobre frete_base + TDA (confirmado com CT-e real: TRT bateu
                # exato em 10% de (frete_base+TDA), não só do frete_base).
                base_trt = frete_base + out.get("tda", 0.0)
                out["trt"] = round(max(base_trt * pct, minimo), 2)

            emex = c.execute(
                "SELECT 1 FROM taxa_cep WHERE transportadora=? AND tipo='EMEX' AND cep_ini<=? AND cep_fin>=? LIMIT 1",
                (transportadora, cep, cep),
            ).fetchone()
            if emex and cfg.get("emex_pct"):
                # Base do EMEX: valor da NF (padrão, ex.: Fitlog) ou o frete
                # (ex.: TG: 8% sobre o valor do frete, mín. R$25).
                base_emex = frete_base if str(cfg.get("emex_base") or "nf").lower() == "frete" else valor_nf
                out["emex"] = round(max(base_emex * cfg["emex_pct"], cfg.get("emex_min", 0)), 2)

        cc = _dig(cnpj_destinatario)
        if cc and cfg.get("tde_pct"):
            # casa por CNPJ completo (14) ou raiz (8) — a lista às vezes traz a raiz
            dest = c.execute(
                "SELECT 1 FROM taxa_dest WHERE transportadora=? AND (cnpj=? OR cnpj=?) LIMIT 1",
                (transportadora, cc, cc[:8]),
            ).fetchone()
            if dest:
                out["tde"] = round(max(frete_base * cfg["tde_pct"], cfg.get("tde_min", 0)), 2)
        return out
    finally:
        c.close()
