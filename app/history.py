"""
Histórico persistente de auditorias de CTe (SQLite, sem dependência externa).

Cada CTe é gravado por chave_cte (chave de acesso, única por documento) — reauditar
o mesmo CTe atualiza o registro em vez de duplicar, mas preserva a data da
primeira auditoria e as observações manuais já anotadas.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

DB_PATH = os.getenv("AUDIT_HISTORY_DB", str(Path(__file__).parent.parent / "tables" / "audit_history.db"))

_COLUMNS = [
    "chave_cte", "arquivo", "numero_cte", "data_emissao", "mes_referencia",
    "transportadora_cnpj", "transportadora_nome_cte", "transportadora_tabela",
    "origem", "destino", "peso_kg", "valor_mercadoria", "valor_cobrado",
    "valor_tabela", "diferenca", "diferenca_pct", "status", "motivo", "tipo_operacao",
    "tipo_operacao_manual", "numero_nf",
    "destinatario_cnpj",
    "charges_tabela", "componentes_cte", "observacoes_auditor",
    "auditado_em", "primeira_auditoria_em",
    "aprovacao_status", "aprovado_por", "aprovado_em", "justificativa",
]

# Ciclo de aprovação: CTe "OK" (bate com a tabela) é aprovado direto, sem passar
# por humano. Qualquer outro status fica PENDENTE até alguém decidir manualmente.
APROVACAO_AUTOMATICA = "AUTOMATICA"
APROVACAO_PENDENTE = "PENDENTE"
APROVACAO_APROVADO_MANUAL = "APROVADO_MANUAL"
APROVACAO_REPROVADO_MANUAL = "REPROVADO_MANUAL"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_history (
            chave_cte TEXT PRIMARY KEY,
            arquivo TEXT,
            numero_cte TEXT,
            data_emissao TEXT,
            mes_referencia TEXT,
            transportadora_cnpj TEXT,
            transportadora_nome_cte TEXT,
            transportadora_tabela TEXT,
            origem TEXT,
            destino TEXT,
            peso_kg REAL,
            valor_mercadoria REAL,
            valor_cobrado REAL,
            valor_tabela REAL,
            diferenca REAL,
            diferenca_pct REAL,
            status TEXT,
            motivo TEXT,
            charges_tabela TEXT,
            componentes_cte TEXT,
            observacoes_auditor TEXT DEFAULT '',
            auditado_em TEXT,
            primeira_auditoria_em TEXT
        )
    """)
    _ensure_columns(conn)
    conn.commit()
    conn.close()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Migração leve para bancos já existentes (mesmo padrão do app.auth)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_history)").fetchall()}
    if "aprovacao_status" not in cols:
        conn.execute("ALTER TABLE audit_history ADD COLUMN aprovacao_status TEXT")
        # Registros já existentes: OK -> automática; qualquer outra coisa -> pendente.
        conn.execute(f"UPDATE audit_history SET aprovacao_status = '{APROVACAO_AUTOMATICA}' WHERE status = 'OK'")
        conn.execute(
            f"UPDATE audit_history SET aprovacao_status = '{APROVACAO_PENDENTE}' "
            "WHERE aprovacao_status IS NULL"
        )
    if "aprovado_por" not in cols:
        conn.execute("ALTER TABLE audit_history ADD COLUMN aprovado_por TEXT")
    if "aprovado_em" not in cols:
        conn.execute("ALTER TABLE audit_history ADD COLUMN aprovado_em TEXT")
    if "justificativa" not in cols:
        conn.execute("ALTER TABLE audit_history ADD COLUMN justificativa TEXT")
    if "tipo_operacao" not in cols:
        conn.execute("ALTER TABLE audit_history ADD COLUMN tipo_operacao TEXT")
    if "tipo_operacao_manual" not in cols:
        # Override manual do tipo de operação (marcação humana vence a automática).
        conn.execute("ALTER TABLE audit_history ADD COLUMN tipo_operacao_manual TEXT")
    if "numero_nf" not in cols:
        conn.execute("ALTER TABLE audit_history ADD COLUMN numero_nf TEXT")
    if "destinatario_cnpj" not in cols:
        conn.execute("ALTER TABLE audit_history ADD COLUMN destinatario_cnpj TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_audit_destinatario ON audit_history(destinatario_cnpj)")


def _mes_referencia(data_emissao: Optional[str]) -> str:
    if data_emissao and len(data_emissao) >= 7:
        return data_emissao[:7]  # "2026-07-21T..." -> "2026-07"
    return datetime.now().strftime("%Y-%m")


def save_result(result) -> None:
    """result: app.auditor.AuditResult (ou objeto com os mesmos atributos).

    Aprovação: CTe com status OK é aprovado automaticamente. Qualquer outro
    status fica PENDENTE de decisão manual. Devolução/reentrega (tipo_operacao
    preenchido) NUNCA aprova automático, mesmo quando bate 100%/50% da ida —
    é dinheiro saindo por uma regra à parte da tabela normal, sempre passa por
    humano. Se o CTe já tinha sido decidido manualmente e a reauditoria não
    mudou nem o status nem a diferença, a decisão anterior (aprovado/reprovado,
    por quem, justificativa) é mantida — só reabre pra revisão quando o
    resultado realmente muda.
    """
    now = datetime.now().isoformat(timespec="seconds")
    chave = result.chave_cte or f"SEMCHAVE::{result.arquivo}::{now}"
    mes = _mes_referencia(result.data_emissao)
    tipo_operacao = getattr(result, "tipo_operacao", None)

    # CT-e já lançado numa fatura (app.auditor._fatura_do_cte): reimportação
    # bloqueada — não escreve NADA, a linha existente fica exatamente como
    # estava. O motivo de rejeição só viaja na resposta HTTP desta chamada.
    if result.status == "JA_LANCADO":
        return

    conn = _get_conn()
    existing = conn.execute(
        "SELECT primeira_auditoria_em, observacoes_auditor, status, diferenca, "
        "aprovacao_status, aprovado_por, aprovado_em, justificativa "
        "FROM audit_history WHERE chave_cte = ?",
        (chave,),
    ).fetchone()

    # Evento de cancelamento (app.auditor._audit_cancelamento): é um EVENTO, não
    # um CT-e (sem rota/valor/NF) — só marca o CT-e já auditado como cancelado
    # (update mínimo, preserva os dados originais do CT-e pra trilha de
    # auditoria) em vez de sobrescrever a linha inteira com campos vazios.
    if result.status == "CANCELADO" and existing is not None:
        conn.execute(
            "UPDATE audit_history SET status = ?, motivo = ?, aprovacao_status = ?, auditado_em = ? "
            "WHERE chave_cte = ?",
            ("CANCELADO", result.motivo, APROVACAO_AUTOMATICA, now, chave),
        )
        conn.commit()
        conn.close()
        return
    primeira = existing["primeira_auditoria_em"] if existing else now
    obs = existing["observacoes_auditor"] if existing else ""

    aprovado_por = existing["aprovado_por"] if existing else None
    aprovado_em = existing["aprovado_em"] if existing else None
    justificativa = existing["justificativa"] if existing else None

    resultado_inalterado = (
        existing is not None
        and existing["status"] == result.status
        and existing["diferenca"] == result.diferenca
        and existing["aprovacao_status"] in (APROVACAO_APROVADO_MANUAL, APROVACAO_REPROVADO_MANUAL)
    )
    if resultado_inalterado:
        aprovacao_status = existing["aprovacao_status"]
    elif tipo_operacao:
        aprovacao_status = APROVACAO_PENDENTE
        aprovado_por, aprovado_em, justificativa = None, None, None
    elif result.status == "OK":
        aprovacao_status = APROVACAO_AUTOMATICA
        aprovado_por, aprovado_em, justificativa = None, None, None
    else:
        aprovacao_status = APROVACAO_PENDENTE
        aprovado_por, aprovado_em, justificativa = None, None, None

    conn.execute("""
        INSERT INTO audit_history (
            chave_cte, arquivo, numero_cte, data_emissao, mes_referencia,
            transportadora_cnpj, transportadora_nome_cte, transportadora_tabela,
            origem, destino, peso_kg, valor_mercadoria, valor_cobrado,
            valor_tabela, diferenca, diferenca_pct, status, motivo, tipo_operacao, numero_nf,
            destinatario_cnpj,
            charges_tabela, componentes_cte, observacoes_auditor,
            auditado_em, primeira_auditoria_em,
            aprovacao_status, aprovado_por, aprovado_em, justificativa
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(chave_cte) DO UPDATE SET
            arquivo=excluded.arquivo, numero_cte=excluded.numero_cte,
            data_emissao=excluded.data_emissao, mes_referencia=excluded.mes_referencia,
            transportadora_cnpj=excluded.transportadora_cnpj,
            transportadora_nome_cte=excluded.transportadora_nome_cte,
            transportadora_tabela=excluded.transportadora_tabela,
            origem=excluded.origem, destino=excluded.destino, peso_kg=excluded.peso_kg,
            valor_mercadoria=excluded.valor_mercadoria, valor_cobrado=excluded.valor_cobrado,
            valor_tabela=excluded.valor_tabela, diferenca=excluded.diferenca,
            diferenca_pct=excluded.diferenca_pct, status=excluded.status, motivo=excluded.motivo,
            tipo_operacao=excluded.tipo_operacao, numero_nf=excluded.numero_nf,
            destinatario_cnpj=excluded.destinatario_cnpj,
            charges_tabela=excluded.charges_tabela, componentes_cte=excluded.componentes_cte,
            auditado_em=excluded.auditado_em,
            aprovacao_status=excluded.aprovacao_status, aprovado_por=excluded.aprovado_por,
            aprovado_em=excluded.aprovado_em, justificativa=excluded.justificativa
    """, (
        chave, result.arquivo, result.numero_cte, result.data_emissao, mes,
        result.transportadora_cnpj, result.transportadora_nome_cte, result.transportadora_tabela,
        result.origem, result.destino, result.peso_kg, result.valor_mercadoria, result.valor_cobrado,
        result.valor_tabela, result.diferenca, result.diferenca_pct, result.status, result.motivo,
        tipo_operacao, getattr(result, "numero_nf", None), getattr(result, "destinatario_cnpj", None),
        json.dumps(result.charges_tabela, ensure_ascii=False) if result.charges_tabela else None,
        json.dumps(result.componentes_cte, ensure_ascii=False) if result.componentes_cte else None,
        obs, now, primeira,
        aprovacao_status, aprovado_por, aprovado_em, justificativa,
    ))
    conn.commit()
    conn.close()


def set_aprovacao(chave_cte: str, decisao: str, aprovado_por: str, justificativa: str) -> Optional[Dict]:
    """decisao: 'APROVADO' ou 'REPROVADO'. Só se aplica a CTes fora do fluxo
    automático (status != OK) — CTe OK já está aprovado desde a auditoria."""
    status_novo = APROVACAO_APROVADO_MANUAL if decisao == "APROVADO" else APROVACAO_REPROVADO_MANUAL
    now = datetime.now().isoformat(timespec="seconds")
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE audit_history SET aprovacao_status = ?, aprovado_por = ?, aprovado_em = ?, justificativa = ? "
        "WHERE chave_cte = ?",
        (status_novo, aprovado_por, now, justificativa, chave_cte),
    )
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        return None
    row = conn.execute("SELECT * FROM audit_history WHERE chave_cte = ?", (chave_cte,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def atualizar_vinculo_manual(
    chave_cte: str, status: str, motivo: str,
    valor_tabela: Optional[float], diferenca: Optional[float], diferenca_pct: Optional[float],
) -> Optional[Dict]:
    """Devolução/reentrega vinculada manualmente à ida (app.auditor.vincular_ida_manual,
    quando o usuário digita o nº da NF porque a busca automática não achou).
    Só toca nos campos do resultado do cálculo — o resto do registro (dados
    do CT-e, observações) fica intacto. aprovacao_status sempre volta pra
    PENDENTE: devolução/reentrega nunca aprova sozinha, mesmo casada manual
    (mesma regra do save_result — ver histórico do porquê)."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE audit_history SET status = ?, motivo = ?, valor_tabela = ?, diferenca = ?, "
        "diferenca_pct = ?, aprovacao_status = ?, aprovado_por = NULL, aprovado_em = NULL, "
        "justificativa = NULL, auditado_em = ? WHERE chave_cte = ?",
        (status, motivo, valor_tabela, diferenca, diferenca_pct, APROVACAO_PENDENTE, now, chave_cte),
    )
    conn.commit()
    if cur.rowcount == 0:
        conn.close()
        return None
    row = conn.execute("SELECT * FROM audit_history WHERE chave_cte = ?", (chave_cte,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def _row_to_dict(row: sqlite3.Row) -> Dict:
    d = dict(row)
    d["charges_tabela"] = json.loads(d["charges_tabela"]) if d["charges_tabela"] else None
    d["componentes_cte"] = json.loads(d["componentes_cte"]) if d["componentes_cte"] else None
    return d


def _apply_status_filter(where: str, params: list, status: Optional[str]) -> str:
    """status='OUTROS' é um sentinel do dashboard: tudo que não é OK nem DIVERGENTE
    (spot, não mapeada, sem tabela, sem cobertura, erro leitura)."""
    if status == "OUTROS":
        return where + " AND status NOT IN ('OK', 'DIVERGENTE')"
    if status:
        params.append(status)
        return where + " AND status = ?"
    return where


def get_by_chave(chave_cte: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM audit_history WHERE chave_cte = ?", (chave_cte,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def _history_where(mes, transportadora, status, search, aprovacao):
    """Cláusula WHERE compartilhada por query_history()/count_history() — filtro
    único, sem divergência entre a página exibida e a contagem total."""
    q = " WHERE 1=1"
    params: list = []
    if mes:
        q += " AND mes_referencia = ?"
        params.append(mes)
    if transportadora:
        q += " AND (transportadora_tabela = ? OR transportadora_nome_cte LIKE ?)"
        params.extend([transportadora, f"%{transportadora}%"])
    q = _apply_status_filter(q, params, status)
    if aprovacao:
        q += " AND aprovacao_status = ?"
        params.append(aprovacao)
    if search:
        s = f"%{search}%"
        q += " AND (arquivo LIKE ? OR numero_cte LIKE ? OR chave_cte LIKE ?)"
        params.extend([s, s, s])
    return q, params


def query_history(
    mes: Optional[str] = None,
    transportadora: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
    aprovacao: Optional[str] = None,
    offset: int = 0,
) -> List[Dict]:
    where, params = _history_where(mes, transportadora, status, search, aprovacao)
    # Pendentes sempre no topo (precisam de decisão humana), dentro disso mais recente primeiro.
    q = ("SELECT * FROM audit_history" + where +
         " ORDER BY (CASE WHEN aprovacao_status = 'PENDENTE' THEN 0 ELSE 1 END),"
         " data_emissao DESC, auditado_em DESC LIMIT ? OFFSET ?")
    conn = _get_conn()
    rows = conn.execute(q, params + [limit, max(0, int(offset or 0))]).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def count_history(
    mes: Optional[str] = None,
    transportadora: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    aprovacao: Optional[str] = None,
) -> int:
    """Total de registros do histórico para os filtros dados (base do pager)."""
    where, params = _history_where(mes, transportadora, status, search, aprovacao)
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM audit_history" + where, params).fetchone()[0]
    conn.close()
    return total


def update_notes(chave_cte: str, observacoes: str) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE audit_history SET observacoes_auditor = ? WHERE chave_cte = ?",
        (observacoes, chave_cte),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


_TIPOS_OPERACAO_VALIDOS = {"REENTREGA", "DEVOLUCAO", "NORMAL"}


def set_tipo_operacao_manual(chave_cte: str, tipo: Optional[str]) -> bool:
    """Marca manualmente o tipo de operação de um CTe (override do automático).
    tipo: 'REENTREGA' | 'DEVOLUCAO' | 'NORMAL' (força remessa normal) | None/'' (limpa
    o override, voltando ao detectado automaticamente)."""
    t = (tipo or "").strip().upper() or None
    if t is not None and t not in _TIPOS_OPERACAO_VALIDOS:
        raise ValueError(f"Tipo inválido: {tipo}. Use REENTREGA, DEVOLUCAO, NORMAL ou vazio.")
    # 'NORMAL' é gravado literalmente: força remessa mesmo que o automático diga o contrário.
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE audit_history SET tipo_operacao_manual = ? WHERE chave_cte = ?",
        (t, chave_cte),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def delete_entry(chave_cte: str) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM audit_history WHERE chave_cte = ?", (chave_cte,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def summary_by_month() -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute("""
        SELECT mes_referencia,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'DIVERGENTE' THEN 1 ELSE 0 END) AS divergentes,
               SUM(CASE WHEN status = 'DIVERGENTE' THEN ABS(diferenca) ELSE 0 END) AS valor_divergente_total,
               SUM(valor_cobrado) AS valor_cobrado_total
        FROM audit_history
        WHERE status != 'CANCELADO'
        GROUP BY mes_referencia
        ORDER BY mes_referencia DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_meses() -> List[str]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT mes_referencia FROM audit_history ORDER BY mes_referencia DESC"
    ).fetchall()
    conn.close()
    return [r["mes_referencia"] for r in rows]


def indicadores(
    mes: Optional[str] = None,
    transportadora: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict:
    """Agregados estilo BI para o dashboard: KPIs gerais + série por mês + ranking por transportadora.
    Exclui CANCELADO — CT-e cancelado foi substituído por outro, contar os dois
    dobraria o valor cobrado."""
    conn = _get_conn()
    where = "WHERE status != 'CANCELADO'"
    params: list = []
    if mes:
        where += " AND mes_referencia = ?"
        params.append(mes)
    if transportadora:
        where += " AND (transportadora_tabela = ? OR transportadora_nome_cte LIKE ?)"
        params.extend([transportadora, f"%{transportadora}%"])
    where = _apply_status_filter(where, params, status)

    kpi = conn.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN status = 'DIVERGENTE' THEN 1 ELSE 0 END) AS divergente,
               SUM(valor_cobrado) AS valor_cobrado_total,
               SUM(CASE WHEN valor_tabela IS NOT NULL THEN valor_tabela ELSE 0 END) AS valor_tabela_total,
               SUM(CASE WHEN status = 'DIVERGENTE' THEN ABS(diferenca) ELSE 0 END) AS valor_divergencia_total
        FROM audit_history {where}
    """, params).fetchone()

    por_mes = conn.execute(f"""
        SELECT mes_referencia,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN status = 'DIVERGENTE' THEN 1 ELSE 0 END) AS divergente,
               SUM(valor_cobrado) AS valor_cobrado,
               SUM(CASE WHEN valor_tabela IS NOT NULL THEN valor_tabela ELSE 0 END) AS valor_tabela,
               SUM(CASE WHEN status = 'DIVERGENTE' THEN ABS(diferenca) ELSE 0 END) AS valor_divergencia
        FROM audit_history {where}
        GROUP BY mes_referencia
        ORDER BY mes_referencia
    """, params).fetchall()

    por_transportadora = conn.execute(f"""
        SELECT COALESCE(NULLIF(transportadora_tabela, ''), NULLIF(transportadora_nome_cte, ''), 'Desconhecida') AS transportadora,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'DIVERGENTE' THEN 1 ELSE 0 END) AS divergente,
               SUM(valor_cobrado) AS valor_cobrado,
               SUM(CASE WHEN status = 'DIVERGENTE' THEN ABS(diferenca) ELSE 0 END) AS valor_divergencia
        FROM audit_history {where}
        GROUP BY transportadora
        ORDER BY valor_divergencia DESC, total DESC
    """, params).fetchall()

    conn.close()

    def _num(v):
        return v if v is not None else 0

    kpi_d = dict(kpi) if kpi else {}
    total = _num(kpi_d.get("total"))
    ok = _num(kpi_d.get("ok"))
    divergente = _num(kpi_d.get("divergente"))
    outros = total - ok - divergente

    return {
        "kpis": {
            "total_ctes": total,
            "ctes_ok": ok,
            "ctes_divergente": divergente,
            "ctes_outros": outros,
            "pct_divergente": round(divergente / total * 100, 1) if total else 0,
            "valor_cobrado_total": round(_num(kpi_d.get("valor_cobrado_total")), 2),
            "valor_tabela_total": round(_num(kpi_d.get("valor_tabela_total")), 2),
            "valor_divergencia_total": round(_num(kpi_d.get("valor_divergencia_total")), 2),
        },
        "por_mes": [
            {
                "mes": r["mes_referencia"],
                "total": _num(r["total"]),
                "ok": _num(r["ok"]),
                "divergente": _num(r["divergente"]),
                "outros": _num(r["total"]) - _num(r["ok"]) - _num(r["divergente"]),
                "valor_cobrado": round(_num(r["valor_cobrado"]), 2),
                "valor_tabela": round(_num(r["valor_tabela"]), 2),
                "valor_divergencia": round(_num(r["valor_divergencia"]), 2),
            }
            for r in por_mes
        ],
        "por_transportadora": [
            {
                "transportadora": r["transportadora"],
                "total": _num(r["total"]),
                "divergente": _num(r["divergente"]),
                "valor_cobrado": round(_num(r["valor_cobrado"]), 2),
                "valor_divergencia": round(_num(r["valor_divergencia"]), 2),
            }
            for r in por_transportadora
        ],
    }


_FORMULA_LEAD_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_cell(value):
    """Neutraliza injeção de fórmula: campos vindos do XML do CTe (não confiáveis)
    podem começar com =, +, -, @ e o Excel/Sheets interpretaria como fórmula."""
    if isinstance(value, str) and value and value[0] in _FORMULA_LEAD_CHARS:
        return "'" + value
    return value


def export_csv(rows: List[Dict]) -> str:
    buf = io.StringIO()
    fieldnames = [c for c in _COLUMNS if c not in ("charges_tabela", "componentes_cte")]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore", delimiter=";")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: _sanitize_cell(v) for k, v in r.items()})
    return buf.getvalue()


_STATUS_LABEL = {
    "OK": "OK",
    "DIVERGENTE": "Divergente",
    "SPOT": "Spot / sem tabela",
    "SEM_VALOR_COBRADO": "Sem valor cobrado",
    "TRANSPORTADORA_NAO_MAPEADA": "Não mapeada",
    "SEM_TABELA": "Sem tabela",
    "SEM_COBERTURA": "Sem cobertura",
    "ERRO_LEITURA": "Erro leitura",
    "DEVOLUCAO_SEM_IDA": "Devolução sem ida encontrada",
    "REENTREGA_SEM_ORIGEM": "Reentrega sem CT-e de origem encontrado",
    "CANCELADO": "Cancelado",
    "JA_LANCADO": "Já lançado na fatura",
}

APROVACAO_LABEL = {
    APROVACAO_AUTOMATICA: "Automática (CTe OK)",
    APROVACAO_PENDENTE: "Pendente",
    APROVACAO_APROVADO_MANUAL: "Aprovado manualmente",
    APROVACAO_REPROVADO_MANUAL: "Reprovado manualmente",
}

_STATUS_FILL = {
    "OK": "C6EFCE",
    "DIVERGENTE": "FFC7CE",
    "SPOT": "D9D9D9",
    "SEM_VALOR_COBRADO": "FFEB9C",
    "TRANSPORTADORA_NAO_MAPEADA": "FFEB9C",
    "SEM_TABELA": "FFEB9C",
    "SEM_COBERTURA": "FFEB9C",
    "ERRO_LEITURA": "FFC7CE",
    "DEVOLUCAO_SEM_IDA": "FFEB9C",
    "REENTREGA_SEM_ORIGEM": "FFEB9C",
    "CANCELADO": "D9D9D9",
    "JA_LANCADO": "FFEB9C",
}


def export_xlsx(rows: List[Dict], filtros: Optional[Dict] = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Relatório de Auditoria"

    header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    title_font = Font(bold=True, size=14)
    money_fmt = '"R$" #,##0.00'

    ws["A1"] = "Relatório de Auditoria de Frete"
    ws["A1"].font = title_font
    ws["A2"] = f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    filtros = {k: v for k, v in (filtros or {}).items() if v}
    if filtros:
        ws["A3"] = "Filtros: " + ", ".join(f"{k}={v}" for k, v in filtros.items())

    total = len(rows)
    ok = sum(1 for r in rows if r["status"] == "OK")
    divergentes = [r for r in rows if r["status"] == "DIVERGENTE"]
    valor_divergente = sum(abs(r["diferenca"] or 0) for r in divergentes)
    valor_cobrado_total = sum(r["valor_cobrado"] or 0 for r in rows)

    ws["A5"] = "Total de CTes"
    ws["B5"] = total
    ws["A6"] = "OK"
    ws["B6"] = ok
    ws["A7"] = "Divergentes"
    ws["B7"] = len(divergentes)
    ws["A8"] = "Valor total em divergência"
    ws["B8"] = valor_divergente
    ws["B8"].number_format = money_fmt
    ws["A9"] = "Valor total cobrado (todos os CTes)"
    ws["B9"] = valor_cobrado_total
    ws["B9"].number_format = money_fmt
    for r in range(5, 10):
        ws[f"A{r}"].font = Font(bold=True)

    headers = [
        ("data_emissao", "Emissão"), ("numero_cte", "Nº CTe"), ("arquivo", "Arquivo"),
        ("transportadora_nome_cte", "Transportadora (CTe)"), ("transportadora_tabela", "Transportadora (Tabela)"),
        ("numero_nf", "NF"), ("origem", "Origem"), ("destino", "Destino"), ("peso_kg", "Peso (kg)"),
        ("valor_mercadoria", "Valor Mercadoria"), ("valor_cobrado", "Valor Cobrado"),
        ("valor_tabela", "Valor Tabela"), ("diferenca", "Diferença"), ("diferenca_pct", "Diferença %"),
        ("status", "Status"), ("tipo_operacao", "Tipo"), ("motivo", "Motivo"),
        ("aprovacao_status", "Aprovação"), ("aprovado_por", "Aprovado/Reprovado por"),
        ("justificativa", "Justificativa"), ("observacoes_auditor", "Observações"),
    ]
    header_row = 11
    for col_idx, (_, label) in enumerate(headers, start=1):
        c = ws.cell(row=header_row, column=col_idx, value=label)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center")

    money_cols = {"valor_mercadoria", "valor_cobrado", "valor_tabela", "diferenca"}
    for row_idx, r in enumerate(rows, start=header_row + 1):
        for col_idx, (key, _) in enumerate(headers, start=1):
            val = r.get(key)
            if key == "data_emissao" and val:
                val = val[:10]
            if key == "status":
                val = _STATUS_LABEL.get(val, val)
            if key == "tipo_operacao":
                val = {"DEVOLUCAO": "Devolução", "REENTREGA": "Reentrega"}.get(val, "Normal")
            if key == "aprovacao_status":
                val = APROVACAO_LABEL.get(val, val)
            val = _sanitize_cell(val)
            c = ws.cell(row=row_idx, column=col_idx, value=val)
            if key in money_cols and isinstance(val, (int, float)):
                c.number_format = money_fmt
        status_fill = _STATUS_FILL.get(r.get("status"))
        if status_fill:
            fill = PatternFill(start_color=status_fill, end_color=status_fill, fill_type="solid")
            for col_idx in range(1, len(headers) + 1):
                ws.cell(row=row_idx, column=col_idx).fill = fill

    widths = [12, 9, 32, 26, 20, 16, 16, 10, 14, 14, 14, 12, 12, 16, 40, 20, 20, 35, 30]
    for col_idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = w
    ws.freeze_panes = f"A{header_row + 1}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
