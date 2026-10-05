"""
Romaneio de Expedição — o "portão de entrada" do ciclo de frete.

Fluxo (decidido com o usuário):
  1. O operador sobe os XMLs das NF-e (faturadas no Olist/Tiny). Cada nota entra
     numa FILA (tabela `notas` com `romaneio_id` nulo) — nada é vinculado ainda.
  2. Na fila, as NFs aparecem agrupadas por transportadora. O operador cria um
     romaneio por transportadora e vai adicionando as NFs que quiser; pode
     adicionar mais depois ou remover (volta pra fila).
  3. Cada romaneio gera o impresso/PDF (identidade Volix) com dados da empresa,
     transportadora, motorista/veículo/placa e áreas de assinatura/canhoto.

Também aceita importar um romaneio já pronto (PDF do Olist — layout fixo) via
`parse_pdf_bytes` (provisório; a calibrar sob amostra real — as NFs reconhecidas
caem na fila para o operador montar/conferir o romaneio).

Gancho de integração: `coverage()` cruza as NFs do romaneio com o histórico de
auditoria (`app.history`) apontando quais já têm CTe emitido e quais estão
"aguardando CTe". Como o histórico guarda o CTe (não a chave da NF), o casamento
é HEURÍSTICO (transportadora + UF + valor da carga ≈ valor da NF, na competência)
e vem rotulado como "provável" — nunca sobrescreve nada.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app import nfe_parser

try:
    from app import cadastro
except Exception:  # pragma: no cover - cadastro sempre existe, defensivo
    cadastro = None

try:
    from app import tracking
except Exception:  # pragma: no cover
    tracking = None

DB_PATH = os.getenv("ROMANEIO_DB", str(Path(__file__).parent.parent / "tables" / "romaneio.db"))

# Tolerância para casar o valor da carga do CTe com o valor da NF (fração).
COVERAGE_TOL_PCT = float(os.getenv("ROMANEIO_COVERAGE_TOL_PCT", "0.02"))

# Faixas (em dias) para sinalizar NF parada na fila sem romaneio.
DIAS_ATENCAO = int(os.getenv("ROMANEIO_DIAS_ATENCAO", "2"))
DIAS_CRITICO = int(os.getenv("ROMANEIO_DIAS_CRITICO", "4"))

# aberto → liberado (transportadora levou) → encerrado (todas entregues); cancelado à parte.
# 'expedido' mantido por compatibilidade com registros antigos.
SITUACOES = ("aberto", "liberado", "encerrado", "cancelado", "expedido")


# ─── Banco ───────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS romaneios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT NOT NULL,
            data_expedicao TEXT,
            transportadora TEXT,
            transportadora_cnpj TEXT,
            emitente_nome TEXT,
            emitente_cnpj TEXT,
            emitente_ie TEXT,
            emitente_endereco TEXT,
            emitente_fone TEXT,
            veiculo TEXT DEFAULT '',
            placa TEXT DEFAULT '',
            motorista TEXT DEFAULT '',
            situacao TEXT DEFAULT 'aberto',
            observacoes TEXT DEFAULT '',
            origem_dados TEXT DEFAULT 'xml',
            data_liberacao TEXT,
            custo_estimado REAL,
            custo_calculado_em TEXT,
            criado_em TEXT
        )
    """)
    # A NF vive na FILA (romaneio_id NULL) até ser vinculada a um romaneio.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chave TEXT UNIQUE,
            nf_numero TEXT,
            nf_serie TEXT,
            data_emissao TEXT,
            emitente_nome TEXT,
            emitente_cnpj TEXT,
            emitente_ie TEXT,
            emitente_endereco TEXT,
            emitente_fone TEXT,
            transportadora TEXT,
            transportadora_cnpj TEXT,
            cliente TEXT,
            dest_municipio TEXT,
            dest_uf TEXT,
            dest_cnpj TEXT,
            valor REAL DEFAULT 0,
            peso_bruto REAL,
            peso_liquido REAL,
            m3 REAL,
            volumes INTEGER,
            especie TEXT,
            dest_cep TEXT,
            origem_dados TEXT DEFAULT 'xml',
            romaneio_id INTEGER,
            criado_em TEXT,
            FOREIGN KEY (romaneio_id) REFERENCES romaneios(id) ON DELETE SET NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notas_romaneio ON notas(romaneio_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notas_transp ON notas(transportadora)")
    _migrar_colunas(conn)
    conn.commit()
    conn.close()


def _migrar_colunas(conn: sqlite3.Connection) -> None:
    """Adiciona colunas novas em bancos já existentes (idempotente)."""
    def cols(tbl):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
    notas_cols = cols("notas")
    if "dest_cep" not in notas_cols:
        conn.execute("ALTER TABLE notas ADD COLUMN dest_cep TEXT")
    if "dest_cnpj" not in notas_cols:
        conn.execute("ALTER TABLE notas ADD COLUMN dest_cnpj TEXT")
    if "m3" not in notas_cols:
        conn.execute("ALTER TABLE notas ADD COLUMN m3 REAL")
    for c, tipo in (("situacao", "TEXT"), ("cancelada_em", "TEXT"),
                    ("nf_finalidade", "TEXT"), ("nf_ref_chave", "TEXT")):
        if c not in notas_cols:
            conn.execute(f"ALTER TABLE notas ADD COLUMN {c} {tipo}")
    rom_cols = cols("romaneios")
    for c, tipo in (("data_liberacao", "TEXT"), ("custo_estimado", "REAL"), ("custo_calculado_em", "TEXT"),
                    ("olist_agrupamento_id", "INTEGER")):
        if c not in rom_cols:
            conn.execute(f"ALTER TABLE romaneios ADD COLUMN {c} {tipo}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ─── Resolução de transportadora (via cadastro) ──────────────────────────────

def _mapa_cnpj_para_nome() -> Dict[str, str]:
    mapa: Dict[str, str] = {}
    if cadastro is None:
        return mapa
    try:
        for t in cadastro.list_transportadoras():
            nome = t.get("nome") or ""
            for c in (t.get("cnpjs") or []):
                cc = re.sub(r"\D", "", c or "")
                if cc:
                    mapa[cc] = nome
    except Exception:
        pass
    return mapa


def _resolve_transportadora(cnpj: str, nome_nf: str, mapa: Dict[str, str]) -> str:
    cc = re.sub(r"\D", "", cnpj or "")
    if cc and cc in mapa:
        return mapa[cc]
    nome = (nome_nf or "").strip()
    return nome or "SEM TRANSPORTADORA"


def _data_expedicao(data_emissao: Optional[str]) -> str:
    if data_emissao and len(data_emissao) >= 10:
        return data_emissao[:10]
    return datetime.now().strftime("%Y-%m-%d")


# ─── Fila (pool) de NFs ──────────────────────────────────────────────────────

_NOTA_COLS = [
    "chave", "nf_numero", "nf_serie", "data_emissao",
    "emitente_nome", "emitente_cnpj", "emitente_ie", "emitente_endereco", "emitente_fone",
    "transportadora", "transportadora_cnpj",
    "cliente", "dest_municipio", "dest_uf", "dest_cep", "dest_cnpj", "valor", "peso_bruto", "peso_liquido",
    "m3", "volumes", "especie", "origem_dados", "romaneio_id", "criado_em",
    "nf_finalidade", "nf_ref_chave",
]


def _nota_row(row: sqlite3.Row) -> Dict:
    return {k: row[k] for k in row.keys()}


def _dias_desde(iso: Optional[str]) -> Optional[int]:
    if not iso or len(iso) < 10:
        return None
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (datetime.now().date() - d).days


def _nivel_dias(dias: Optional[int]) -> str:
    if dias is None:
        return "ok"
    if dias >= DIAS_CRITICO:
        return "critico"
    if dias >= DIAS_ATENCAO:
        return "atencao"
    return "ok"


def add_notas_from_xml(files: List[Tuple[str, bytes]]) -> Dict:
    """Lê os XMLs de NF-e e adiciona cada nota à FILA (dedupe por chave)."""
    notas, erros = nfe_parser.parse_many(files)
    mapa = _mapa_cnpj_para_nome()
    conn = _get_conn()
    adicionadas = 0
    duplicadas = 0
    try:
        for nf in notas:
            chave = re.sub(r"\D", "", nf.chave or "")
            if not chave:
                erros.append({"arquivo": nf.arquivo, "erro": "NF-e sem chave de acesso válida."})
                continue
            ja = conn.execute("SELECT id, romaneio_id FROM notas WHERE chave=?", (chave,)).fetchone()
            if ja is not None:
                duplicadas += 1
                continue
            transp = _resolve_transportadora(nf.transportadora_cnpj, nf.transportadora_nome, mapa)
            # m³ da carga (para o peso cubado) — via dimensões dos produtos no Olist.
            nf_m3 = None
            try:
                from app import olist
                nf_m3 = olist.m3_da_nota(getattr(nf, "itens", None))
            except Exception:
                nf_m3 = None
            conn.execute(
                f"INSERT INTO notas ({', '.join(_NOTA_COLS)}) VALUES ({', '.join(['?']*len(_NOTA_COLS))})",
                (
                    chave, nf.numero, nf.serie, nf.data_emissao,
                    nf.emitente_nome, nf.emitente_cnpj, nf.emitente_ie, nf.emitente_endereco, nf.emitente_fone,
                    transp, re.sub(r"\D", "", nf.transportadora_cnpj or ""),
                    nf.dest_nome, nf.dest_municipio, (nf.dest_uf or "").upper(), nf.dest_cep,
                    re.sub(r"\D", "", nf.dest_doc or ""),
                    round(nf.valor_nf or 0.0, 2), nf.peso_bruto, nf.peso_liquido,
                    nf_m3, nf.volumes, nf.especie, "xml", None, _now(),
                    getattr(nf, "finalidade", "") or "", getattr(nf, "ref_chave", "") or "",
                ),
            )
            adicionadas += 1
        conn.commit()
    finally:
        conn.close()
    return {
        "adicionadas": adicionadas,
        "duplicadas": duplicadas,
        "erros": erros,
        "resumo": {"arquivos": len(files), "notas": len(notas),
                   "adicionadas": adicionadas, "duplicadas": duplicadas, "com_erro": len(erros)},
    }


def chaves_existentes() -> set:
    """Todas as chaves de NF-e já conhecidas (na fila OU já em romaneio).

    Usado pela integração com o Olist para não baixar de novo o XML de uma
    nota que já entrou — a dedupe final continua sendo por chave no insert.
    """
    conn = _get_conn()
    try:
        return {r[0] for r in conn.execute("SELECT chave FROM notas").fetchall() if r[0]}
    finally:
        conn.close()


def notas_sem_peso() -> List[Dict]:
    """NFs já cadastradas (fila ou romaneio) que ficaram SEM peso e/ou volume.

    Servem à re-sincronização: relê-se o XML no Olist e completa-se só esses
    campos (sem re-adicionar a NF). Casos típicos: notas importadas antes de a
    expedição preencher peso/volume no Olist."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, chave, nf_numero FROM notas "
            "WHERE (peso_bruto IS NULL OR peso_bruto = 0 OR volumes IS NULL OR volumes = 0) "
            "AND (situacao IS NULL OR situacao NOT IN ('cancelada')) "
            "ORDER BY criado_em"
        ).fetchall()
        return [{"id": r["id"], "chave": r["chave"], "nf_numero": r["nf_numero"]} for r in rows]
    finally:
        conn.close()


def atualizar_peso_volume(chave: str, peso_bruto=None, peso_liquido=None,
                          volumes=None, especie: str = "", m3=None) -> Dict:
    """Completa peso/volume de uma NF já cadastrada (match por chave), SEM tocar
    em nenhum outro campo nem re-adicionar. Só grava valor que veio preenchido —
    um XML que continua sem <vol> não zera o que porventura já exista.

    Retorna {"encontrada": bool, "atualizada": bool, "tinha_dado": bool}."""
    chave = re.sub(r"\D", "", chave or "")
    if not chave:
        return {"encontrada": False, "atualizada": False, "tinha_dado": False}
    sets, params = [], []
    if peso_bruto not in (None, 0, 0.0):
        sets.append("peso_bruto = ?"); params.append(peso_bruto)
    if peso_liquido not in (None, 0, 0.0):
        sets.append("peso_liquido = ?"); params.append(peso_liquido)
    if volumes not in (None, 0):
        sets.append("volumes = ?"); params.append(int(volumes))
    if especie:
        sets.append("especie = ?"); params.append(especie)
    if m3 not in (None, 0, 0.0):
        sets.append("m3 = ?"); params.append(m3)
    tinha_dado = bool(sets)
    conn = _get_conn()
    try:
        existe = conn.execute("SELECT 1 FROM notas WHERE chave = ?", (chave,)).fetchone()
        if existe is None:
            return {"encontrada": False, "atualizada": False, "tinha_dado": tinha_dado}
        if not tinha_dado:
            return {"encontrada": True, "atualizada": False, "tinha_dado": False}
        params.append(chave)
        conn.execute(f"UPDATE notas SET {', '.join(sets)} WHERE chave = ?", params)
        conn.commit()
        return {"encontrada": True, "atualizada": True, "tinha_dado": True}
    finally:
        conn.close()


def notas_na_fila() -> List[Dict]:
    """Notas atualmente na fila (sem romaneio, ativas) — só chave e nº p/ reconciliar."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT chave, nf_numero FROM notas WHERE romaneio_id IS NULL "
            "AND (situacao IS NULL OR situacao NOT IN ('cancelada', 'expedida'))"
        ).fetchall()
        return [{"chave": r[0], "nf_numero": r[1]} for r in rows]
    finally:
        conn.close()


def marcar_expedida(chave: str) -> bool:
    """Tira uma nota da fila porque a expedição dela foi fechada/agrupada no Olist.

    Mantém o registro (situacao='expedida') para auditoria e para não reimportar.
    """
    chave = re.sub(r"\D", "", chave or "")
    if not chave:
        return False
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE notas SET situacao='expedida', cancelada_em=? "
            "WHERE chave=? AND romaneio_id IS NULL AND (situacao IS NULL OR situacao NOT IN ('cancelada','expedida'))",
            (_now(), chave),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def marcar_cancelada(chave: str) -> Optional[Dict]:
    """Estorna uma NF cancelada no Olist. Marca situacao='cancelada' (sai da fila).

    Retorna dados para o alerta se a nota existia e ainda não estava cancelada;
    None se não existe ou já estava cancelada (nada a fazer).
    """
    chave = re.sub(r"\D", "", chave or "")
    if not chave:
        return None
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT nf_numero, transportadora, romaneio_id, situacao FROM notas WHERE chave=?",
            (chave,),
        ).fetchone()
        if row is None or (row["situacao"] or "") == "cancelada":
            return None
        conn.execute("UPDATE notas SET situacao='cancelada', cancelada_em=? WHERE chave=?", (_now(), chave))
        conn.commit()
        return {
            "chave": chave,
            "nf_numero": row["nf_numero"],
            "transportadora": row["transportadora"],
            "em_romaneio": row["romaneio_id"] is not None,
            "romaneio_id": row["romaneio_id"],
        }
    finally:
        conn.close()


def alertas_cancelamento() -> List[Dict]:
    """NFs canceladas no Olist que ainda estão DENTRO de um romaneio ABERTO —
    exigem ação manual (tirar da carga antes de liberar). Só alerta enquanto dá
    para agir: uma vez que o romaneio é liberado (transportadora levou), a carga
    já saiu e não há o que tirar, então esses casos deixam de aparecer."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT n.id AS nota_id, n.chave, n.nf_numero, n.transportadora, n.cancelada_em, "
            "       n.romaneio_id, r.numero AS romaneio_numero "
            "FROM notas n JOIN romaneios r ON r.id = n.romaneio_id "
            "WHERE n.situacao='cancelada' AND COALESCE(r.situacao, 'aberto') = 'aberto' "
            "ORDER BY n.cancelada_em DESC"
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()


def resolver_alertas_cancelamento(nota_ids: Optional[List[int]] = None) -> Dict:
    """Tira do romaneio as NFs canceladas no Olist que ainda estão em carga
    (romaneio_id → NULL) e limpa o provisório delas na Torre — assim o alerta
    some sozinho (o alerta só lista situacao='cancelada' AND romaneio_id NÃO nulo).
    `nota_ids=None` resolve TODAS as pendentes; caso contrário, só as informadas.
    Retorna {"resolvidas": n, "alertas": [...restantes]}."""
    conn = _get_conn()
    try:
        q = ("SELECT id FROM notas WHERE situacao='cancelada' AND romaneio_id IS NOT NULL")
        params: list = []
        if nota_ids:
            marks = ",".join("?" * len(nota_ids))
            q += f" AND id IN ({marks})"
            params = list(nota_ids)
        alvos = [r["id"] for r in conn.execute(q, params).fetchall()]
        if not alvos:
            return {"resolvidas": 0, "alertas": alertas_cancelamento()}
        pares = _cnpj_numero_das_notas(conn, alvos)
        marks = ",".join("?" * len(alvos))
        conn.execute(f"UPDATE notas SET romaneio_id=NULL WHERE id IN ({marks})", alvos)
        conn.commit()
    finally:
        conn.close()
    _limpar_torre_das_notas(pares)
    return {"resolvidas": len(alvos), "alertas": alertas_cancelamento()}


def _totais(itens: List[Dict]) -> Dict:
    return {
        "qtd_nfs": len(itens),
        "valor_total": round(sum(i.get("valor") or 0 for i in itens), 2),
        "peso_bruto": round(sum(i.get("peso_bruto") or 0 for i in itens), 3),
        "volumes": int(sum(i.get("volumes") or 0 for i in itens)),
    }


def list_pool(transportadora: Optional[str] = None) -> Dict:
    """NFs na fila (sem romaneio), com os grupos por transportadora prontos p/
    virar romaneio."""
    conn = _get_conn()
    # Fila = só o que NÃO tem romaneio. Blindagem extra: se por qualquer motivo
    # existir uma linha solta com a MESMA chave de uma NF já vinculada, ela não
    # aparece na fila (mostra apenas o que de fato não está em romaneio).
    q = ("SELECT * FROM notas WHERE romaneio_id IS NULL "
         "AND (situacao IS NULL OR situacao NOT IN ('cancelada', 'expedida')) "
         "AND (chave IS NULL OR chave = '' OR chave NOT IN "
         "     (SELECT chave FROM notas WHERE romaneio_id IS NOT NULL AND chave IS NOT NULL AND chave <> ''))")
    params: list = []
    if transportadora:
        q += " AND transportadora = ?"
        params.append(transportadora)
    q += " ORDER BY transportadora, data_emissao, nf_numero"
    rows = [_nota_row(r) for r in conn.execute(q, params).fetchall()]
    conn.close()

    # Dias em aberto: quanto tempo a NF está parada sem romaneio (base = emissão,
    # fallback = entrada na fila). Sinaliza atraso na expedição.
    for n in rows:
        dias = _dias_desde(n.get("data_emissao"))
        if dias is None:
            dias = _dias_desde(n.get("criado_em"))
        n["dias_aberto"] = dias
        n["nivel_dias"] = _nivel_dias(dias)

    grupos: Dict[str, Dict] = {}
    for n in rows:
        t = n.get("transportadora") or "SEM TRANSPORTADORA"
        g = grupos.setdefault(t, {"transportadora": t, "itens": [], "nota_ids": []})
        g["itens"].append(n)
        g["nota_ids"].append(n["id"])
    saida = []
    for g in sorted(grupos.values(), key=lambda x: x["transportadora"]):
        g["totais"] = _totais(g["itens"])
        dias_vals = [i["dias_aberto"] for i in g["itens"] if i.get("dias_aberto") is not None]
        g["dias_max"] = max(dias_vals) if dias_vals else None
        g["nivel_dias"] = _nivel_dias(g["dias_max"])
        saida.append(g)

    return {"notas": rows, "grupos": saida, "totais": _totais(rows)}


def delete_nota(nota_id: int) -> bool:
    """Descarta uma NF da fila (só se ainda não estiver em um romaneio)."""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM notas WHERE id=? AND romaneio_id IS NULL", (nota_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ─── Numeração ───────────────────────────────────────────────────────────────

def _proximo_numero(conn: sqlite3.Connection) -> str:
    ano = datetime.now().year
    prefixo = f"ROM-{ano}-"
    row = conn.execute(
        "SELECT numero FROM romaneios WHERE numero LIKE ? ORDER BY id DESC LIMIT 1",
        (prefixo + "%",),
    ).fetchone()
    seq = 1
    if row and row["numero"]:
        m = re.search(r"(\d+)$", row["numero"])
        if m:
            seq = int(m.group(1)) + 1
    return f"{prefixo}{seq:04d}"


# ─── Criar romaneio a partir da fila ─────────────────────────────────────────

def _notas_da_fila(conn: sqlite3.Connection, nota_ids: List[int]) -> List[sqlite3.Row]:
    if not nota_ids:
        return []
    marks = ",".join("?" * len(nota_ids))
    return conn.execute(
        f"SELECT * FROM notas WHERE id IN ({marks}) AND romaneio_id IS NULL "
        "AND (situacao IS NULL OR situacao NOT IN ('cancelada', 'expedida'))", nota_ids
    ).fetchall()


def create_romaneio(dados: Dict, nota_ids: Optional[List[int]] = None) -> Optional[Dict]:
    """Cria um romaneio e vincula as NFs escolhidas (que estejam na fila).

    Se `nota_ids` vier vazio mas `transportadora` estiver preenchida, pega TODAS
    as NFs da fila daquela transportadora (atalho "criar romaneio por
    transportadora")."""
    conn = _get_conn()
    try:
        transp = (dados.get("transportadora") or "").strip()
        if not nota_ids and transp:
            rows = conn.execute(
                "SELECT * FROM notas WHERE romaneio_id IS NULL AND transportadora=? "
                "AND (situacao IS NULL OR situacao NOT IN ('cancelada', 'expedida'))", (transp,)
            ).fetchall()
        else:
            rows = _notas_da_fila(conn, nota_ids or [])
        if not rows:
            raise ValueError("Nenhuma NF disponível na fila para este romaneio.")

        primeira = rows[0]
        if not transp:
            transp = primeira["transportadora"] or ""
        data_exp = (dados.get("data_expedicao") or "").strip() or _data_expedicao(primeira["data_emissao"])

        numero = (dados.get("numero") or "").strip() or _proximo_numero(conn)
        cur = conn.execute(
            """INSERT INTO romaneios
               (numero, data_expedicao, transportadora, transportadora_cnpj,
                emitente_nome, emitente_cnpj, emitente_ie, emitente_endereco, emitente_fone,
                veiculo, placa, motorista, situacao, observacoes, origem_dados, criado_em)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                numero, data_exp, transp, primeira["transportadora_cnpj"] or "",
                primeira["emitente_nome"] or "", primeira["emitente_cnpj"] or "",
                primeira["emitente_ie"] or "", primeira["emitente_endereco"] or "",
                primeira["emitente_fone"] or "",
                dados.get("veiculo") or "", dados.get("placa") or "", dados.get("motorista") or "",
                "aberto", dados.get("observacoes") or "", dados.get("origem_dados") or "xml", _now(),
            ),
        )
        rid = cur.lastrowid
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE notas SET romaneio_id=? WHERE id IN ({marks})", [rid, *ids])
        conn.commit()
    finally:
        conn.close()
    return get_romaneio(rid)


def add_notas(rid: int, nota_ids: List[int]) -> Optional[Dict]:
    """Vincula NFs da fila a um romaneio existente. A NF **adota a transportadora
    do romaneio** — permite incluir manualmente NF de outra transportadora
    (fisicamente ela passa a seguir com a transportadora do romaneio)."""
    conn = _get_conn()
    r = conn.execute("SELECT transportadora, transportadora_cnpj FROM romaneios WHERE id=?", (rid,)).fetchone()
    if r is None:
        conn.close()
        return None
    rows = _notas_da_fila(conn, nota_ids or [])
    ids = [x["id"] for x in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE notas SET romaneio_id=?, transportadora=?, transportadora_cnpj=? WHERE id IN ({marks})",
            [rid, r["transportadora"] or "", r["transportadora_cnpj"] or "", *ids],
        )
        conn.commit()
    conn.close()
    return get_romaneio(rid)


def _cnpj_numero_das_notas(conn: sqlite3.Connection, ids: List[int]) -> List[Tuple[str, str]]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT emitente_cnpj, nf_numero FROM notas WHERE id IN ({marks})", ids
    ).fetchall()
    return [(r["emitente_cnpj"] or "", r["nf_numero"] or "") for r in rows]


def _limpar_torre_das_notas(pares: List[Tuple[str, str]]) -> None:
    """Remove os provisórios da Torre das NFs (usado ao mover/remover de romaneio)."""
    if tracking is None:
        return
    for cnpj, numero in pares:
        try:
            tracking.remover_provisorio_nf(cnpj, numero)
        except Exception:
            pass


def remove_notas(rid: int, nota_ids: List[int]) -> Optional[Dict]:
    """Tira NFs do romaneio, devolvendo-as à fila (e limpando a Torre delas)."""
    conn = _get_conn()
    r = conn.execute("SELECT id FROM romaneios WHERE id=?", (rid,)).fetchone()
    if r is None:
        conn.close()
        return None
    pares = []
    if nota_ids:
        marks = ",".join("?" * len(nota_ids))
        pares = _cnpj_numero_das_notas(
            conn, [x["id"] for x in conn.execute(
                f"SELECT id FROM notas WHERE romaneio_id=? AND id IN ({marks})", [rid, *nota_ids]
            ).fetchall()])
        conn.execute(
            f"UPDATE notas SET romaneio_id=NULL WHERE romaneio_id=? AND id IN ({marks})",
            [rid, *nota_ids],
        )
        conn.commit()
    conn.close()
    _limpar_torre_das_notas(pares)
    return get_romaneio(rid)


def mover_notas(nota_ids: List[int], destino_rid: int) -> Optional[Dict]:
    """Move NFs (de qualquer romaneio ou da fila) para o romaneio `destino_rid`,
    adotando a transportadora do destino. Serve para 'tirar a NF do romaneio da
    transp X e colocar no da transp Y'. Limpa o provisório da Torre das NFs
    (re-sincronize o destino se ele já estiver liberado)."""
    conn = _get_conn()
    dest = conn.execute(
        "SELECT transportadora, transportadora_cnpj FROM romaneios WHERE id=?", (destino_rid,)
    ).fetchone()
    if dest is None or not nota_ids:
        conn.close()
        return None
    marks = ",".join("?" * len(nota_ids))
    pares = _cnpj_numero_das_notas(conn, list(nota_ids))
    conn.execute(
        f"UPDATE notas SET romaneio_id=?, transportadora=?, transportadora_cnpj=? WHERE id IN ({marks})",
        [destino_rid, dest["transportadora"] or "", dest["transportadora_cnpj"] or "", *nota_ids],
    )
    conn.commit()
    conn.close()
    _limpar_torre_das_notas(pares)
    return get_romaneio(destino_rid)


def set_transportadora_nota(nota_id: int, transportadora: str, cnpj: str = "") -> bool:
    """Reclassifica a transportadora de uma NF que está NA FILA (sem romaneio) —
    para agrupá-la sob outra transportadora antes de criar o romaneio."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE notas SET transportadora=?, transportadora_cnpj=? WHERE id=? AND romaneio_id IS NULL",
        ((transportadora or "").strip() or "SEM TRANSPORTADORA", re.sub(r"\D", "", cnpj or ""), nota_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ─── Consultas ───────────────────────────────────────────────────────────────

def _romaneio_row(row: sqlite3.Row) -> Dict:
    return {k: row[k] for k in row.keys()}


def _itens_do_romaneio(conn: sqlite3.Connection, rid: int) -> List[Dict]:
    rows = conn.execute(
        "SELECT * FROM notas WHERE romaneio_id=? ORDER BY nf_numero", (rid,)
    ).fetchall()
    return [_nota_row(r) for r in rows]


def list_romaneios(transportadora: Optional[str] = None,
                   situacao: Optional[str] = None,
                   mes: Optional[str] = None,
                   search: Optional[str] = None) -> List[Dict]:
    conn = _get_conn()
    q = "SELECT * FROM romaneios WHERE 1=1"
    params: list = []
    if transportadora:
        q += " AND transportadora = ?"
        params.append(transportadora)
    if situacao:
        q += " AND situacao = ?"
        params.append(situacao)
    if mes:
        q += " AND substr(data_expedicao,1,7) = ?"
        params.append(mes)
    if search:
        s = f"%{search}%"
        q += " AND (numero LIKE ? OR transportadora LIKE ?)"
        params.extend([s, s])
    q += " ORDER BY data_expedicao DESC, id DESC"
    rows = conn.execute(q, params).fetchall()

    saida = []
    reconciliar = []
    for r in rows:
        d = _romaneio_row(r)
        itens = conn.execute(
            "SELECT id, emitente_cnpj, nf_numero, valor, peso_bruto, volumes FROM notas WHERE romaneio_id=?",
            (r["id"],),
        ).fetchall()
        itens = [dict(x) for x in itens]
        d["totais"] = {
            "qtd_nfs": len(itens),
            "valor_total": round(sum(i["valor"] or 0 for i in itens), 2),
            "peso_bruto": round(sum(i["peso_bruto"] or 0 for i in itens), 3),
            "volumes": int(sum(i["volumes"] or 0 for i in itens)),
        }
        entregas = _entregas_dos_itens(itens)
        d["entrega"] = _entrega_info(itens, entregas)
        # Auto-encerramento: todas as NFs entregues e ainda não encerrado/cancelado.
        if d["entrega"]["todas_entregues"] and d.get("situacao") not in ("encerrado", "cancelado"):
            reconciliar.append(r["id"])
            d["situacao"] = "encerrado"
        saida.append(d)

    if reconciliar:
        marks = ",".join("?" * len(reconciliar))
        conn.execute(f"UPDATE romaneios SET situacao='encerrado' WHERE id IN ({marks})", reconciliar)
        conn.commit()
    conn.close()
    return saida


def reconciliar_encerrados() -> int:
    """Encerra romaneios cujas NFs foram TODAS entregues (via Torre/OCOREN).
    Mesma regra do auto-encerramento de list_romaneios, mas chamável na hora —
    usado logo após importar OCOREN para o encerramento refletir imediatamente,
    sem esperar alguém abrir a tela de Romaneio. Retorna quantos foram encerrados."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM romaneios WHERE situacao NOT IN ('encerrado','cancelado')"
        ).fetchall()
        encerrar = []
        for r in rows:
            itens = [dict(x) for x in conn.execute(
                "SELECT id, emitente_cnpj, nf_numero FROM notas WHERE romaneio_id=?", (r["id"],)
            ).fetchall()]
            entregas = _entregas_dos_itens(itens)
            if _entrega_info(itens, entregas)["todas_entregues"]:
                encerrar.append(r["id"])
        if encerrar:
            marks = ",".join("?" * len(encerrar))
            conn.execute(f"UPDATE romaneios SET situacao='encerrado' WHERE id IN ({marks})", encerrar)
            conn.commit()
        return len(encerrar)
    finally:
        conn.close()


def _entregas_dos_itens(itens: List[Dict]) -> Dict[int, str]:
    """{nota_id: data_entrega} para as NFs do romaneio já entregues (via Torre)."""
    if tracking is None or not itens:
        return {}
    por_cnpj: Dict[str, List[Dict]] = {}
    for it in itens:
        cnpj = re.sub(r"\D", "", it.get("emitente_cnpj") or "")
        if cnpj and it.get("nf_numero"):
            por_cnpj.setdefault(cnpj, []).append(it)
    saida: Dict[int, str] = {}
    for cnpj, its in por_cnpj.items():
        try:
            mapa = tracking.datas_entrega_de_nfs(cnpj, [i.get("nf_numero") for i in its])
        except Exception:
            mapa = {}
        for it in its:
            num = str(it.get("nf_numero") or "").lstrip("0")
            if num in mapa:
                saida[it["id"]] = mapa[num]
    return saida


def _entrega_info(itens: List[Dict], entregas: Dict[int, str]) -> Dict:
    n_entregues = sum(1 for it in itens if it["id"] in entregas)
    todas = bool(itens) and n_entregues == len(itens)
    ultima = max(entregas.values()) if entregas else None
    return {"nfs_entregues": n_entregues, "todas_entregues": todas, "ultima_entrega": ultima}


def set_olist_agrupamento(rid: int, agrupamento_id) -> None:
    """Guarda o ID do agrupamento (Expedição) criado no Olist para este romaneio."""
    conn = _get_conn()
    try:
        conn.execute("UPDATE romaneios SET olist_agrupamento_id=? WHERE id=?", (agrupamento_id, rid))
        conn.commit()
    finally:
        conn.close()


def get_romaneio(rid: int) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM romaneios WHERE id=?", (rid,)).fetchone()
    if row is None:
        conn.close()
        return None
    d = _romaneio_row(row)
    d["itens"] = _itens_do_romaneio(conn, rid)
    conn.close()
    d["totais"] = _totais(d["itens"])
    d["cobertura"] = coverage(d["transportadora"], d["itens"], d.get("data_expedicao") or "")
    entregas = _entregas_dos_itens(d["itens"])
    for it in d["itens"]:
        it["data_entrega"] = entregas.get(it["id"])
    d["entrega"] = _entrega_info(d["itens"], entregas)
    return d


def update_romaneio(rid: int, campos: Dict) -> Optional[Dict]:
    permitidos = {"situacao", "observacoes", "veiculo", "placa", "motorista",
                  "data_expedicao", "transportadora"}
    sets = []
    params: list = []
    for k, v in campos.items():
        if k in permitidos and v is not None:
            if k == "situacao" and v not in SITUACOES:
                raise ValueError(f"Situação inválida: {v}")
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return get_romaneio(rid)
    params.append(rid)
    conn = _get_conn()
    conn.execute(f"UPDATE romaneios SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    conn.close()
    if campos.get("situacao") == "cancelado" and tracking is not None:
        try:
            tracking.remover_romaneio(rid)  # cancelou → sai da Torre
        except Exception:
            pass
    return get_romaneio(rid)


def delete_romaneio(rid: int, descartar_notas: bool = False) -> bool:
    """Exclui o romaneio. Por padrão DEVOLVE as NFs à fila (não perde XML)."""
    conn = _get_conn()
    exists = conn.execute("SELECT id FROM romaneios WHERE id=?", (rid,)).fetchone()
    if exists is None:
        conn.close()
        return False
    if descartar_notas:
        conn.execute("DELETE FROM notas WHERE romaneio_id=?", (rid,))
    else:
        conn.execute("UPDATE notas SET romaneio_id=NULL WHERE romaneio_id=?", (rid,))
    conn.execute("DELETE FROM romaneios WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    if tracking is not None:
        try:
            tracking.remover_romaneio(rid)  # tira os provisórios da Torre
        except Exception:
            pass
    return True


# ─── #2 Liberar romaneio (transportadora levou) → Torre de Controle ──────────

def liberar_romaneio(rid: int, prazos: Optional[Dict[int, Dict]] = None) -> Optional[Dict]:
    """Marca o romaneio como LIBERADO (expedido, mesmo sem CTe) e registra suas
    NFs na Torre como entregas provisórias. `prazos`: {nota_id: {'prazo_dias':int,
    'data_prevista':str}} calculado pelo motor (opcional — sem ele a Torre mostra
    a entrega sem previsão de prazo)."""
    r = get_romaneio(rid)
    if r is None:
        return None
    prazos = prazos or {}
    conn = _get_conn()
    # Preserva a liberação ORIGINAL: só carimba a data/hora atual na PRIMEIRA
    # liberação. Re-sincronizar/re-liberar um romaneio já liberado NÃO pode
    # reescrever a data para hoje (bagunçaria o prazo e a coluna "Liberação").
    atual = conn.execute("SELECT data_liberacao FROM romaneios WHERE id=?", (rid,)).fetchone()
    liberacao = (atual["data_liberacao"] if atual and atual["data_liberacao"] else _now())
    conn.execute(
        "UPDATE romaneios SET situacao='liberado', data_liberacao=? WHERE id=? AND situacao<>'cancelado'",
        (liberacao, rid),
    )
    conn.commit()
    conn.close()

    if tracking is not None:
        itens_tk = []
        for it in r["itens"]:
            # NF cancelada (no Olist) não vira entrega na Torre — mesmo que ainda
            # esteja presa no romaneio aguardando remoção manual (ver
            # alertas_cancelamento). Sem isso, liberar o romaneio ressuscitaria a
            # NF cancelada como entrega provisória "sem CTe".
            if (it.get("situacao") or "") == "cancelada":
                continue
            pz = prazos.get(it["id"], {})
            itens_tk.append({
                "chave": it.get("chave"),
                "nf_numero": it.get("nf_numero"),
                "nf_serie": it.get("nf_serie"),
                "emitente_cnpj": it.get("emitente_cnpj"),
                "dest_municipio": it.get("dest_municipio"),
                "dest_uf": it.get("dest_uf"),
                "prazo_dias": pz.get("prazo_dias"),
                "data_prevista": pz.get("data_prevista"),
            })
        try:
            tracking.registrar_romaneio(
                rid, r["numero"], r["transportadora"], r.get("transportadora_cnpj") or "",
                r.get("data_expedicao"), itens_tk, data_liberacao=liberacao,
            )
        except Exception:
            pass
    return get_romaneio(rid)


def salvar_custo(rid: int, custo_total: float) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE romaneios SET custo_estimado=?, custo_calculado_em=? WHERE id=?",
        (round(custo_total, 2), _now(), rid),
    )
    conn.commit()
    conn.close()


# ─── #4 Cálculo de custo do romaneio (motor de cotação) ──────────────────────

def _norm_nome(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _resolve_tabela(nome: str, cnpj: str, precos: Dict) -> Optional[str]:
    """Casa o nome/CNPJ da transportadora do romaneio com a chave usada nas
    tabelas do motor (nomes do Cadastro). Tolera variações: 'TG_Transportes'→'TG',
    caixa/acentos/pontuação, e resolve por CNPJ cadastrado. Retorna a chave ou None."""
    if not precos:
        return None
    if nome in precos:
        return nome
    # via CNPJ cadastrado na base de acordo
    cc = re.sub(r"\D", "", cnpj or "")
    if cc:
        alvo = _mapa_cnpj_para_nome().get(cc)
        if alvo and alvo in precos:
            return alvo
    # nome normalizado: igualdade, depois prefixo único (ex.: 'tgtransportes'⊃'tg')
    nc = _norm_nome(nome)
    if not nc:
        return None
    exatos = [k for k in precos if _norm_nome(k) == nc]
    if exatos:
        return exatos[0]
    cand = [k for k in precos if len(_norm_nome(k)) >= 2 and
            (nc.startswith(_norm_nome(k)) or _norm_nome(k).startswith(nc))]
    return cand[0] if len(cand) == 1 else None


def _quote_itens(r: Dict, precos: Dict, prazos: Dict) -> Dict:
    """Cota cada NF do romaneio para a transportadora dele (reusa
    rate_calculator.quote_for_carrier). Retorna dict com carrier_ok e a lista
    por NF (custo, prazo_dias, data_prevista, motivo)."""
    from datetime import date
    from app import rate_calculator

    nome_rom = r.get("transportadora") or ""
    carrier = _resolve_tabela(nome_rom, r.get("transportadora_cnpj"), precos)
    precos_data = precos.get(carrier) if carrier else None
    prazos_df = prazos.get(carrier) if carrier else None
    if not precos_data or prazos_df is None:
        disponiveis = ", ".join(sorted(precos.keys())) or "nenhuma"
        return {"carrier_ok": False,
                "motivo_carrier": f"Transportadora '{nome_rom}' sem tabela de preço/prazo cadastrada. Cadastradas: {disponiveis}.",
                "tabela_usada": None, "itens": []}

    # O prazo de entrega conta a partir da DATA DE EXPEDIÇÃO (coleta/despacho da
    # carga), não do dia em que a cotação roda. Assim a previsão não muda conforme
    # o dia em que se abre a tela, e reflete quando a mercadoria realmente saiu.
    hoje = date.today()
    exp = (r.get("data_expedicao") or "")[:10]
    try:
        inicio_prazo = date.fromisoformat(exp) if exp else hoje
    except ValueError:
        inicio_prazo = hoje
    cubagem = float(precos_data.get("meta", {}).get("cubagem_fator", 0) or 0)
    linhas = []
    for it in r.get("itens", []):
        dest_cep = re.sub(r"\D", "", it.get("dest_cep") or "")
        dest_cep = dest_cep.zfill(8) if dest_cep else ""
        city_uf = (it.get("dest_municipio") or "", it.get("dest_uf") or "")
        # Peso taxado = maior entre o peso real e o peso cubado (m³ × cubagem da
        # transportadora), como o CTe cobra (PESO CÁLCULO).
        peso_real = float(it.get("peso_bruto") or 0)
        m3 = float(it.get("m3") or 0)
        peso = max(peso_real, m3 * cubagem) if (m3 and cubagem) else peso_real
        valor = float(it.get("valor") or 0)
        dm, motivo = rate_calculator.quote_for_carrier(
            carrier, precos_data, prazos_df, dest_cep, peso, valor, city_uf=city_uf, today=inicio_prazo,
            cnpj_destinatario=it.get("dest_cnpj"),
        )
        if dm is not None:
            prevista = dm.delivery_estimate_date
            linhas.append({
                "nota_id": it["id"], "nf_numero": it.get("nf_numero"),
                "cliente": it.get("cliente"), "dest_uf": it.get("dest_uf"),
                "ok": True, "custo": round(dm.final_shipping_cost, 2),
                "prazo_dias": dm.delivery_estimate_business_days,
                "data_prevista": prevista.isoformat() if hasattr(prevista, "isoformat") else prevista,
                "motivo": "",
            })
        else:
            linhas.append({
                "nota_id": it["id"], "nf_numero": it.get("nf_numero"),
                "cliente": it.get("cliente"), "dest_uf": it.get("dest_uf"),
                "ok": False, "custo": None, "prazo_dias": None, "data_prevista": None,
                "motivo": motivo,
            })
    return {"carrier_ok": True, "motivo_carrier": "", "tabela_usada": carrier, "itens": linhas}


def calcular_custo(rid: int, precos: Dict, prazos: Dict) -> Optional[Dict]:
    """Estima o custo de frete do romaneio somando a cotação de cada NF; persiste
    o total. Devolve o detalhamento por NF + %frete sobre o valor das NFs."""
    r = get_romaneio(rid)
    if r is None:
        return None
    q = _quote_itens(r, precos, prazos)
    if not q["carrier_ok"]:
        return {"rid": rid, "carrier_ok": False, "motivo": q["motivo_carrier"],
                "custo_total": None, "itens": []}

    itens = q["itens"]
    custo_total = round(sum(l["custo"] or 0 for l in itens if l["ok"]), 2)
    valor_total = r["totais"]["valor_total"]
    cotadas = sum(1 for l in itens if l["ok"])
    salvar_custo(rid, custo_total)
    return {
        "rid": rid, "carrier_ok": True, "numero": r["numero"], "transportadora": r["transportadora"],
        "tabela_usada": q.get("tabela_usada"),
        "custo_total": custo_total, "valor_total": valor_total,
        "pct_frete": round(custo_total / valor_total * 100, 2) if valor_total else None,
        "nfs_cotadas": cotadas, "nfs_total": len(itens),
        "nfs_sem_cotacao": len(itens) - cotadas,
        "itens": itens,
    }


def prazos_por_nota(rid: int, precos: Dict, prazos: Dict) -> Dict[int, Dict]:
    """{nota_id: {'prazo_dias','data_prevista'}} — usado pela liberação para dar
    à Torre a previsão de entrega mesmo antes do CTe."""
    r = get_romaneio(rid)
    if r is None:
        return {}
    q = _quote_itens(r, precos, prazos)
    if not q["carrier_ok"]:
        return {}
    return {l["nota_id"]: {"prazo_dias": l["prazo_dias"], "data_prevista": l["data_prevista"]}
            for l in q["itens"] if l["ok"]}


# ─── Gancho: cobertura de CTe (heurística) ───────────────────────────────────

def _meses_dos_itens(data_expedicao: str, itens: List[Dict]) -> List[str]:
    meses = set()
    if data_expedicao and len(data_expedicao) >= 7:
        meses.add(data_expedicao[:7])
    for it in itens:
        de = it.get("data_emissao")
        if de and len(de) >= 7:
            meses.add(de[:7])
    return sorted(meses) or [datetime.now().strftime("%Y-%m")]


def coverage(transportadora: str, itens: List[Dict], data_expedicao: str = "") -> Dict:
    """Cobertura de CTe por VÍNCULO REAL: uma NF só aparece 'COM_CTE' quando existe
    um CTe de fato auditado que referencia aquela NF (via tracking.ctes_de_nfs,
    alimentado pela Auditoria de CTe). Não usa mais heurística de valor — antes
    isso gerava falso positivo (NFs de mesmo valor/UF casavam com o mesmo CTe)."""
    if not itens:
        return {"com_cte": 0, "aguardando": 0, "itens": []}

    # Vínculo NF→CTe pela chave/número, agrupando por CNPJ emissor.
    ctes: Dict[int, str] = {}
    if tracking is not None:
        por_cnpj: Dict[str, List[Dict]] = {}
        for it in itens:
            cnpj = re.sub(r"\D", "", it.get("emitente_cnpj") or "")
            if cnpj and it.get("nf_numero"):
                por_cnpj.setdefault(cnpj, []).append(it)
        for cnpj, its in por_cnpj.items():
            try:
                mapa = tracking.ctes_de_nfs(cnpj, [i.get("nf_numero") for i in its])
            except Exception:
                mapa = {}
            for it in its:
                num = str(it.get("nf_numero") or "").lstrip("0")
                if num in mapa:
                    ctes[it["id"]] = mapa[num]

    itens_cov = []
    com = 0
    for it in itens:
        cte = ctes.get(it["id"])
        if cte:
            com += 1
        itens_cov.append({
            "nota_id": it.get("id"),
            "nf_numero": it.get("nf_numero"),
            "chave": it.get("chave"),
            "status": "COM_CTE" if cte else "AGUARDANDO",
            "numero_cte": cte or "",
        })
    return {"com_cte": com, "aguardando": len(itens) - com, "itens": itens_cov}


# ─── Impresso / PDF (reportlab) ──────────────────────────────────────────────

def _fmt_money(v) -> str:
    try:
        s = f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "0,00"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_data(iso: str) -> str:
    if iso and len(iso) >= 10:
        try:
            return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return iso[:10]
    return iso or ""


def gerar_pdf(rid: int) -> Optional[bytes]:
    r = get_romaneio(rid)
    if r is None:
        return None

    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable,
    )

    VOLIX = colors.HexColor("#6ba428")
    ESCURO = colors.HexColor("#1f2a1a")
    CINZA = colors.HexColor("#666666")

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"Romaneio {r['numero']}",
    )
    styles = getSampleStyleSheet()
    st_empresa = ParagraphStyle("emp", parent=styles["Normal"], fontSize=9, leading=12, textColor=ESCURO)
    st_small = ParagraphStyle("sm", parent=styles["Normal"], fontSize=8, leading=10, textColor=CINZA)
    st_cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8, leading=10)
    st_title = ParagraphStyle("tit", parent=styles["Normal"], fontSize=16, leading=18,
                              textColor=ESCURO, alignment=2)
    st_num = ParagraphStyle("num", parent=styles["Normal"], fontSize=12, leading=14,
                            textColor=VOLIX, alignment=2)

    elems = []

    empresa_txt = f"<b>{r.get('emitente_nome') or 'Expedidor'}</b>"
    linhas_emp = []
    doc_ie = []
    if r.get("emitente_cnpj"):
        doc_ie.append(f"CNPJ {r['emitente_cnpj']}")
    if r.get("emitente_ie"):
        doc_ie.append(f"IE {r['emitente_ie']}")
    if doc_ie:
        linhas_emp.append(" · ".join(doc_ie))
    if r.get("emitente_endereco"):
        linhas_emp.append(r["emitente_endereco"])
    if r.get("emitente_fone"):
        linhas_emp.append(f"Fone: {r['emitente_fone']}")
    empresa_full = empresa_txt + ("<br/>" + "<br/>".join(linhas_emp) if linhas_emp else "")

    cab = Table(
        [[Paragraph(empresa_full, st_empresa),
          Paragraph("ROMANEIO DE EXPEDIÇÃO", st_title)]],
        colWidths=[110 * mm, 72 * mm],
    )
    cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elems.append(cab)
    elems.append(Spacer(1, 2 * mm))
    elems.append(Paragraph(f"Nº {r['numero']}", st_num))
    elems.append(Spacer(1, 2 * mm))
    elems.append(HRFlowable(width="100%", thickness=1.4, color=VOLIX))
    elems.append(Spacer(1, 3 * mm))

    info = [
        [Paragraph("<b>Transportadora</b>", st_small), Paragraph(r.get("transportadora") or "-", st_cell),
         Paragraph("<b>Data de expedição</b>", st_small), Paragraph(_fmt_data(r.get("data_expedicao")), st_cell)],
        [Paragraph("<b>Motorista</b>", st_small), Paragraph(r.get("motorista") or "________________", st_cell),
         Paragraph("<b>Veículo / Placa</b>", st_small),
         Paragraph((r.get("veiculo") or "____") + "  /  " + (r.get("placa") or "________"), st_cell)],
    ]
    tinfo = Table(info, colWidths=[30 * mm, 61 * mm, 30 * mm, 61 * mm])
    tinfo.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e0e0e0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f4f7ef")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f4f7ef")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ]))
    elems.append(tinfo)
    elems.append(Spacer(1, 4 * mm))

    head = ["#", "NF", "Cliente", "Destino", "Vol.", "Peso (kg)", "Valor (R$)"]
    data = [head]
    for i, it in enumerate(r["itens"], 1):
        destino = it.get("dest_municipio") or ""
        if it.get("dest_uf"):
            destino = f"{destino}/{it['dest_uf']}" if destino else it["dest_uf"]
        data.append([
            str(i),
            Paragraph(str(it.get("nf_numero") or ""), st_cell),
            Paragraph(it.get("cliente") or "", st_cell),
            Paragraph(destino, st_cell),
            str(it.get("volumes") or ""),
            _fmt_money(it.get("peso_bruto")) if it.get("peso_bruto") else "",
            _fmt_money(it.get("valor")),
        ])
    tot = r["totais"]
    data.append([
        "", "", Paragraph("<b>TOTAIS</b>", st_cell), f"{tot['qtd_nfs']} NF(s)",
        str(tot["volumes"]), _fmt_money(tot["peso_bruto"]), _fmt_money(tot["valor_total"]),
    ])

    tbl = Table(data, colWidths=[8 * mm, 20 * mm, 55 * mm, 42 * mm, 12 * mm, 22 * mm, 23 * mm],
                repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), VOLIX),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f7f9f3")]),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef3e4")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elems.append(tbl)
    elems.append(Spacer(1, 10 * mm))

    ass = Table(
        [[Paragraph("_______________________________<br/>Conferente / Expedição", st_small),
          Paragraph("_______________________________<br/>Motorista (recebi as mercadorias e volumes acima)", st_small)]],
        colWidths=[91 * mm, 91 * mm],
    )
    ass.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    elems.append(ass)
    elems.append(Spacer(1, 4 * mm))
    elems.append(Paragraph(
        f"Emitido em {datetime.now().strftime('%d/%m/%Y %H:%M')} · Freight Gateway (Volix) · "
        f"Situação: {r.get('situacao','aberto')}", st_small))

    doc.build(elems)
    return buf.getvalue()


# ─── Export Excel ────────────────────────────────────────────────────────────

def export_xlsx(rid: int) -> Optional[bytes]:
    r = get_romaneio(rid)
    if r is None:
        return None
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Romaneio"
    verde = PatternFill("solid", fgColor="6BA428")
    bold_w = Font(bold=True, color="FFFFFF")

    ws["A1"] = f"Romaneio {r['numero']}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Transportadora: {r.get('transportadora','')}"
    ws["A3"] = f"Data de expedição: {_fmt_data(r.get('data_expedicao'))}"
    ws["A4"] = f"Emitente: {r.get('emitente_nome','')}"

    head = ["#", "NF", "Série", "Chave", "Cliente", "Município", "UF",
            "Volumes", "Peso bruto (kg)", "Valor (R$)"]
    hr = 6
    for c, h in enumerate(head, 1):
        cell = ws.cell(row=hr, column=c, value=h)
        cell.fill = verde
        cell.font = bold_w
    for i, it in enumerate(r["itens"], 1):
        row = hr + i
        vals = [i, it.get("nf_numero"), it.get("nf_serie"), it.get("chave"),
                it.get("cliente"), it.get("dest_municipio"), it.get("dest_uf"),
                it.get("volumes"), it.get("peso_bruto"), it.get("valor")]
        for c, v in enumerate(vals, 1):
            ws.cell(row=row, column=c, value=v)
    tot = r["totais"]
    trow = hr + len(r["itens"]) + 1
    ws.cell(row=trow, column=5, value="TOTAIS").font = Font(bold=True)
    ws.cell(row=trow, column=8, value=tot["volumes"]).font = Font(bold=True)
    ws.cell(row=trow, column=9, value=tot["peso_bruto"]).font = Font(bold=True)
    ws.cell(row=trow, column=10, value=tot["valor_total"]).font = Font(bold=True)

    widths = [5, 12, 7, 46, 32, 20, 6, 9, 15, 14]
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + c)].width = w

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ─── Import de romaneio pronto em PDF (Olist — layout fixo) ───────────────────

class PdfParseError(Exception):
    pass


def extrair_texto_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as e:  # pragma: no cover
        raise PdfParseError(f"Leitor de PDF indisponível: {e}") from e
    from io import BytesIO
    try:
        reader = PdfReader(BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        raise PdfParseError(f"PDF inválido ou ilegível: {e}") from e


def import_pdf(data: bytes, filename: str = "") -> Dict:
    """Importa um romaneio pronto do Olist (PDF, layout fixo). PROVISÓRIO: extrai
    as chaves de acesso (44 díg) do texto e adiciona as NFs reconhecidas à FILA
    (transportadora em branco — o operador confere/agrupa). A calibração fina do
    layout do Olist depende de uma AMOSTRA real (como OCOREN/fatura)."""
    texto = extrair_texto_pdf(data)
    chaves = re.findall(r"\b\d{44}\b", texto)

    conn = _get_conn()
    adicionadas = 0
    duplicadas = 0
    try:
        vistos = set()
        for ch in chaves:
            if ch in vistos:
                continue
            vistos.add(ch)
            if conn.execute("SELECT id FROM notas WHERE chave=?", (ch,)).fetchone():
                duplicadas += 1
                continue
            numero = ch[25:34].lstrip("0") or "0"
            serie = ch[22:25].lstrip("0") or "0"
            conn.execute(
                f"INSERT INTO notas ({', '.join(_NOTA_COLS)}) VALUES ({', '.join(['?']*len(_NOTA_COLS))})",
                (ch, numero, serie, None, "", "", "", "", "",
                 "", "", "", "", "", None, 0.0, None, None, None, "", "pdf", None, _now()),
            )
            adicionadas += 1
        conn.commit()
    finally:
        conn.close()

    erros = []
    if not chaves:
        erros.append({"arquivo": filename,
                      "erro": "Nenhuma NF reconhecida no PDF — envie uma amostra para calibrar o layout do Olist."})
    return {
        "adicionadas": adicionadas, "duplicadas": duplicadas, "erros": erros,
        "resumo": {"arquivos": 1, "notas": len(set(chaves)),
                   "adicionadas": adicionadas, "duplicadas": duplicadas, "com_erro": len(erros)},
        "texto_bruto": texto[:4000],
    }
