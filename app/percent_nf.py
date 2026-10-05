"""
Custo de Frete sobre Faturamento (%NF).

O número que a diretoria pede: quanto do valor faturado (mercadoria transportada)
está sendo gasto com frete. É derivado inteiramente do histórico de auditoria
(app/history.py) — cada CTe já traz o frete cobrado (`valor_cobrado`) e o valor
da mercadoria da NF (`valor_mercadoria`, do vCarga do CTe).

    %NF = Σ(frete cobrado) ÷ Σ(valor da NF) × 100     [ponderado, não média de razões]

Ponderado de propósito: a diretoria quer o percentual do faturamento total, então
somamos os fretes e as NFs do grupo e dividimos — um CTe grande pesa mais que um
pequeno, como deve ser. Só entram no cálculo os CTes com frete > 0 E valor de NF > 0;
os sem valor de mercadoria (vCarga ausente) são contados à parte ("sem valor NF"),
nunca somem do relatório.

Metas POR ESTADO (quanto MENOR, melhor — o oposto do OTD do scorecard):
o %NF cresce com a distância, então cada UF de destino tem sua própria meta
(alvo/atenção), editável no painel e guardada em tables/percent_nf_metas.json.
Um agregado que mistura vários estados (uma transportadora, um mês, o geral) é
colorido por uma **meta efetiva ponderada** pelo valor de NF de cada estado do mix
— quem entrega mais longe recebe, automaticamente, uma meta proporcionalmente maior.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app import history as audit_history

METAS_FILE = os.getenv("PERCENT_NF_METAS_FILE", str(Path(__file__).parent.parent / "tables" / "percent_nf_metas.json"))

# Meta global padrão (usada p/ UF sem meta própria). MENOR é melhor.
DEFAULT_META = float(os.getenv("META_NF_PCT", "15"))
DEFAULT_ATENCAO = float(os.getenv("ATENCAO_NF_PCT", "20"))

# UF sede da operação (Grande Vitória/ES). Frete de VENDA (distribuição) sai daqui;
# frete que se ORIGINA em outra UF é coleta/entrada (compra em fornecedor, ex.: um
# fornecedor em Manaus → ES) e NÃO entra no %NF sobre faturamento — é custo de compra,
# não de venda. Fica contado à parte.
SEDE_UF = os.getenv("SEDE_UF", "ES").strip().upper()

# Escopos do painel.
ESCOPOS = ("distribuicao", "coleta", "todas")
ESCOPO_PADRAO = "distribuicao"


REGIOES = {
    "Norte":        ["AC", "AP", "AM", "PA", "RO", "RR", "TO"],
    "Nordeste":     ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
    "Centro-Oeste": ["DF", "GO", "MT", "MS"],
    "Sudeste":      ["ES", "MG", "RJ", "SP"],
    "Sul":          ["PR", "RS", "SC"],
}
UFS = {uf for ufs in REGIOES.values() for uf in ufs}

# Sementes de meta por região (ponto de partida — origem das tabelas é ES/Grande
# Vitória, então o custo relativo de frete tende a subir com a distância). O usuário
# calibra cada estado no painel; estes valores só evitam começar tudo no mesmo número.
_REGIAO_META_SEED = {
    "Sudeste":      (10.0, 15.0),
    "Sul":          (14.0, 20.0),
    "Centro-Oeste": (16.0, 22.0),
    "Nordeste":     (18.0, 24.0),
    "Norte":        (20.0, 28.0),
}

_metas_cache: Optional[Dict] = None


def _seed_metas() -> Dict:
    por_uf = {}
    for reg, ufs in REGIOES.items():
        meta, atencao = _REGIAO_META_SEED[reg]
        for uf in ufs:
            por_uf[uf] = {"meta": meta, "atencao": atencao}
    return {"default": {"meta": DEFAULT_META, "atencao": DEFAULT_ATENCAO}, "por_uf": por_uf}


def load_metas(force: bool = False) -> Dict:
    """Metas de %NF por UF (+ padrão). Cria o arquivo com sementes por região no
    1º uso. Falha de leitura/escrita cai nas sementes em memória, sem quebrar."""
    global _metas_cache
    if _metas_cache is not None and not force:
        return _metas_cache
    p = Path(METAS_FILE)
    data = None
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if data is None:
        data = _seed_metas()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    data.setdefault("default", {"meta": DEFAULT_META, "atencao": DEFAULT_ATENCAO})
    data.setdefault("por_uf", {})
    _metas_cache = data
    return data


def save_metas(default: Dict, por_uf: Dict) -> Dict:
    """Grava as metas (default + por UF) e atualiza o cache. Mantém só UFs válidas."""
    def _pair(d: Dict) -> Dict:
        meta = round(float(d.get("meta", DEFAULT_META)), 2)
        atencao = round(float(d.get("atencao", DEFAULT_ATENCAO)), 2)
        # Atenção nunca abaixo do alvo (senão a faixa amarela some).
        if atencao < meta:
            atencao = meta
        return {"meta": meta, "atencao": atencao}

    limpo = {uf: _pair(v) for uf, v in (por_uf or {}).items() if uf in UFS and isinstance(v, dict)}
    data = {"default": _pair(default or {}), "por_uf": limpo}
    Path(METAS_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(METAS_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    global _metas_cache
    _metas_cache = data
    return data


def meta_de_uf(uf: Optional[str]) -> Tuple[float, float]:
    """(alvo, atenção) de %NF para a UF de destino; cai no padrão se não configurada."""
    m = load_metas()
    if uf and uf in m["por_uf"]:
        p = m["por_uf"][uf]
        return float(p["meta"]), float(p["atencao"])
    d = m["default"]
    return float(d["meta"]), float(d["atencao"])


def status_nf(pct: Optional[float], meta: Optional[float], atencao: Optional[float]) -> Optional[str]:
    """'verde'/'amarelo'/'vermelho' pela faixa de %NF (menor = melhor), contra a
    meta informada (por UF ou efetiva do mix). None quando não há base p/ calcular."""
    if pct is None or meta is None or atencao is None:
        return None
    if pct <= meta:
        return "verde"
    if pct <= atencao:
        return "amarelo"
    return "vermelho"


def _nome_transp(r: Dict) -> str:
    return (r.get("transportadora_tabela") or r.get("transportadora_nome_cte") or "Desconhecida").strip() or "Desconhecida"


def _uf_de(r: Dict) -> Optional[str]:
    """UF de destino a partir do campo destino ('Município/UF')."""
    d = r.get("destino") or ""
    if "/" in d:
        uf = d.rsplit("/", 1)[-1].strip().upper()
        if uf in UFS:
            return uf
    return None


def _uf_origem(r: Dict) -> Optional[str]:
    """UF de origem a partir do campo origem ('Município/UF')."""
    o = r.get("origem") or ""
    if "/" in o:
        uf = o.rsplit("/", 1)[-1].strip().upper()
        if uf in UFS:
            return uf
    return None


def is_coleta(r: Dict) -> bool:
    """Coleta/entrada = frete que NÃO sai da sede (origem em outra UF — fornecedor
    fora do ES, ex.: Manaus → ES). É custo de compra, não de venda: fica fora do
    %NF sobre faturamento e é contado à parte. Origem ausente/ilegível ou na própria
    sede conta como distribuição (o caso normal — 96% dos CTes saem do ES)."""
    o = _uf_origem(r)
    return o is not None and o != SEDE_UF


def tipo_efetivo(r: Dict) -> Optional[str]:
    """Tipo de operação vigente do CTe: a marcação MANUAL vence a automática.
    'REENTREGA' | 'DEVOLUCAO' | None (remessa normal)."""
    v = (r.get("tipo_operacao_manual") or r.get("tipo_operacao") or "").strip().upper()
    return v or None


def _custo_especial(rows_all: List[Dict], transp: Optional[str], ufs_sel: set) -> Dict:
    """Custo (frete cobrado) de reentrega e devolução, sobre TODOS os CTes do mês.
    Respeita os filtros de transportadora e UF (casando destino OU origem — a
    devolução volta pro hub, então a UF relevante dela é a de origem)."""
    def _match_uf(r):
        return (not ufs_sel) or (_uf_de(r) in ufs_sel) or (_uf_origem(r) in ufs_sel)

    out = {"REENTREGA": [0.0, 0], "DEVOLUCAO": [0.0, 0]}
    for r in rows_all:
        t = tipo_efetivo(r)
        if t not in out:
            continue
        if transp and _nome_transp(r) != transp:
            continue
        if not _match_uf(r):
            continue
        out[t][0] += r.get("valor_cobrado") or 0.0
        out[t][1] += 1
    return {
        "custo_reentrega": round(out["REENTREGA"][0], 2),
        "ctes_reentrega": out["REENTREGA"][1],
        "custo_devolucao": round(out["DEVOLUCAO"][0], 2),
        "ctes_devolucao": out["DEVOLUCAO"][1],
    }


def _mes_de(r: Dict) -> str:
    return r.get("mes_referencia") or (r.get("data_emissao") or "")[:7]


def _agrega(rows: List[Dict]) -> Dict:
    """Consolida uma lista de CTes em métricas de %NF.
    frete_base/valor_nf são o par que forma o %NF (só CTes com frete e NF > 0);
    frete_total soma o frete de TODOS os CTes (número financeiro real).
    meta_efetiva/atencao_efetiva são a média das metas de cada UF do mix, ponderada
    pelo valor de NF — a régua justa para colorir grupos que cruzam vários estados."""
    ctes = 0
    com_nf = 0
    frete_base = 0.0   # numerador do %NF
    valor_nf = 0.0     # denominador do %NF
    frete_total = 0.0  # todo frete cobrado (inclui CTes sem valor de NF)
    meta_w = 0.0       # Σ(valor_nf_i × meta_uf_i) — p/ meta efetiva ponderada
    atencao_w = 0.0

    for r in rows:
        ctes += 1
        vc = r.get("valor_cobrado") or 0.0
        vm = r.get("valor_mercadoria") or 0.0
        frete_total += vc
        if vc > 0 and vm > 0:
            com_nf += 1
            frete_base += vc
            valor_nf += vm
            meta_uf, atencao_uf = meta_de_uf(_uf_de(r))
            meta_w += vm * meta_uf
            atencao_w += vm * atencao_uf

    pct = round(frete_base / valor_nf * 100, 2) if valor_nf else None
    meta_ef = round(meta_w / valor_nf, 2) if valor_nf else None
    atencao_ef = round(atencao_w / valor_nf, 2) if valor_nf else None
    return {
        "ctes": ctes,
        "com_valor_nf": com_nf,
        "sem_valor_nf": ctes - com_nf,
        "frete_total": round(frete_total, 2),
        "frete_base": round(frete_base, 2),
        "valor_nf": round(valor_nf, 2),
        "pct_nf": pct,
        "ticket_frete": round(frete_total / ctes, 2) if ctes else None,
        "meta_efetiva": meta_ef,
        "atencao_efetiva": atencao_ef,
        "status": status_nf(pct, meta_ef, atencao_ef),
    }


def _por_estado(rows: List[Dict], uf_fn=None) -> List[Dict]:
    """Agrega por UF. `uf_fn` define de onde tirar a UF (padrão: destino). No modo
    de análise de DEVOLUÇÃO usamos a UF de ORIGEM (de onde a carga voltou = o cliente)."""
    uf_fn = uf_fn or _uf_de
    por_uf: Dict[str, List[Dict]] = {}
    for r in rows:
        uf = uf_fn(r)
        if uf:
            por_uf.setdefault(uf, []).append(r)
    linhas = []
    for uf in sorted(por_uf):
        ag = _agrega(por_uf[uf])
        ag["uf"] = uf
        # Numa única UF a meta efetiva é a própria meta do estado — reexpõe explícita.
        meta_uf, atencao_uf = meta_de_uf(uf)
        ag["meta"] = meta_uf
        ag["atencao"] = atencao_uf
        linhas.append(ag)
    return linhas


def _serie_mensal(rows: List[Dict]) -> List[Dict]:
    """%NF por mês (referência), mais antigo → mais recente, para o gráfico de linha."""
    por_mes: Dict[str, List[Dict]] = {}
    for r in rows:
        m = _mes_de(r)
        if m:
            por_mes.setdefault(m, []).append(r)
    serie = []
    for m in sorted(por_mes):
        ag = _agrega(por_mes[m])
        serie.append({
            "mes": m,
            "pct_nf": ag["pct_nf"],
            "frete_base": ag["frete_base"],
            "valor_nf": ag["valor_nf"],
            "frete_total": ag["frete_total"],
            "ctes": ag["ctes"],
            "meta_efetiva": ag["meta_efetiva"],
            "atencao_efetiva": ag["atencao_efetiva"],
            "status": ag["status"],
        })
    return serie


def percent_nf(mes: Optional[str] = None, uf: Optional[str] = None,
               escopo: str = ESCOPO_PADRAO, transp: Optional[str] = None,
               op: Optional[str] = None) -> Dict:
    """%NF consolidado: resumo geral + por transportadora (com série mensal em cada)
    + série mensal geral + por estado (UF). `mes` no formato 'YYYY-MM'; `uf` filtra
    o painel por estado de destino (o mapa por estado continua com todas as UFs do mês).

    `escopo` separa frete de venda de frete de compra:
      • 'distribuicao' (padrão) — só frete que sai da sede (venda). É o %NF sobre faturamento.
      • 'coleta'                — só coletas/entrada (origem fora da sede).
      • 'todas'                 — tudo junto.
    `op` (REENTREGA|DEVOLUCAO) liga o MODO ANÁLISE DE OPERAÇÃO: o painel inteiro
    (mapa, cards, por transportadora, série) passa a mostrar só os CTes daquele tipo,
    para ver quem/onde gera mais reentrega/devolução. Nesse modo o `escopo` é ignorado
    e, para DEVOLUÇÃO, a UF usada é a de ORIGEM (de onde a carga voltou = o cliente).
    As coletas são SEMPRE agregadas à parte em `coletas_resumo`."""
    # CTes ilegíveis (ERRO_LEITURA) não são documentos de frete válidos: não devem
    # contar em "CTes considerados", inflar "sem valor de NF" nem diluir o ticket médio.
    rows_all = [r for r in audit_history.query_history(mes=mes, limit=100000)
                if r.get("status") != "ERRO_LEITURA"]

    coletas_rows = [r for r in rows_all if is_coleta(r)]
    distrib_rows = [r for r in rows_all if not is_coleta(r)]

    op = (op or "").strip().upper() or None
    if op not in ("REENTREGA", "DEVOLUCAO"):
        op = None

    escopo = (escopo or ESCOPO_PADRAO).strip().lower()
    if escopo not in ESCOPOS:
        escopo = ESCOPO_PADRAO

    if op:
        # Modo análise de operação: só os CTes desse tipo (escopo não se aplica).
        rows_mes = [r for r in rows_all if tipo_efetivo(r) == op]
        # Devolução volta pro hub → a UF que interessa é a de origem (o cliente).
        uf_fn = _uf_origem if op == "DEVOLUCAO" else _uf_de
    else:
        rows_mes = {"coleta": coletas_rows, "todas": rows_all}.get(escopo, distrib_rows)
        uf_fn = _uf_de

    # Coletas de entrada, sempre agregadas à parte (nível do mês, sem filtro de UF)
    # para o painel mostrar quanto de frete de compra ficou FORA do %NF de venda.
    coletas_resumo = _agrega(coletas_rows)

    # Lista de transportadoras do conjunto atual (para o dropdown), antes de qualquer filtro.
    lista_transportadoras = sorted({_nome_transp(r) for r in rows_mes})

    # Filtro por transportadora: afeta TUDO (KPIs, mapa, série, tabela) — clicar numa
    # transportadora no painel vira este filtro global, então todos os cards reagem.
    transp = (transp or "").strip() or None
    if transp:
        rows_mes = [r for r in rows_mes if _nome_transp(r) == transp]

    # Mapa por estado é sobre o mês/escopo/transportadora, mostrando todas as UFs.
    por_estado = _por_estado(rows_mes, uf_fn)

    # `uf` aceita uma ou várias UFs separadas por vírgula (seleção múltipla no mapa).
    ufs_sel = {u.strip().upper() for u in (uf or "").replace(";", ",").split(",") if u.strip()}
    ufs_sel = {u for u in ufs_sel if u in UFS}
    rows = [r for r in rows_mes if uf_fn(r) in ufs_sel] if ufs_sel else rows_mes

    # Custo de reentrega e devolução: sobre TODOS os CTes do mês (fora do split
    # coleta/venda, pois devolução é de entrada e reentrega de saída), sincronizado
    # aos cliques de transportadora e UF. Sempre visível nos dois cards.
    custo_especial = _custo_especial(rows_all, transp, ufs_sel)

    geral = _agrega(rows)

    por_transp: Dict[str, List[Dict]] = {}
    for r in rows:
        por_transp.setdefault(_nome_transp(r), []).append(r)

    linhas = []
    for nome, rs in por_transp.items():
        ag = _agrega(rs)
        ag["transportadora"] = nome
        ag["serie"] = _serie_mensal(rs)
        linhas.append(ag)

    # Maior participação no frete total primeiro (é o que a diretoria olha:
    # quem consome mais frete). Sem base de %NF vai ao fim.
    linhas.sort(key=lambda x: (-x["frete_total"], x["pct_nf"] is None, x["pct_nf"] or 0))

    avaliadas = [l for l in linhas if l["pct_nf"] is not None]
    resumo = dict(geral)
    resumo.update({
        "transportadoras": len(linhas),
        "no_alvo": sum(1 for l in avaliadas if l["status"] == "verde"),
        "em_atencao": sum(1 for l in avaliadas if l["status"] == "amarelo"),
        "criticas": sum(1 for l in avaliadas if l["status"] == "vermelho"),
        **custo_especial,
    })

    return {
        "metas": load_metas(),
        "meta": resumo["meta_efetiva"],       # referência geral (mix de estados)
        "atencao": resumo["atencao_efetiva"],
        "mes": mes,
        "uf": ",".join(sorted(ufs_sel)) if ufs_sel else None,
        "escopo": escopo,
        "op": op,
        "sede_uf": SEDE_UF,
        "transp": transp,
        "lista_transportadoras": lista_transportadoras,
        "resumo": resumo,
        "coletas_resumo": coletas_resumo,
        "por_transportadora": linhas,
        "serie_mensal": _serie_mensal(rows),
        "por_estado": por_estado,
    }


def list_meses() -> List[str]:
    """Meses com CTes auditados, mais recentes primeiro."""
    return audit_history.list_meses()


# ─── Exportação Excel ────────────────────────────────────────────────────────

def export_xlsx(data: Dict) -> bytes:
    """Relatório de %NF: Resumo geral + Por transportadora + Evolução mensal + Por estado.
    Cores usam a meta efetiva de cada linha (por estado / mix), não um limiar único."""
    import io
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from app.history import _sanitize_cell

    header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    money_fmt = '"R$" #,##0.00'
    pct_fmt = "0.00"

    def _cabecalho(ws, headers, row):
        for col_idx, label in enumerate(headers, start=1):
            c = ws.cell(row=row, column=col_idx, value=label)
            c.fill = header_fill
            c.font = header_font
            c.alignment = Alignment(horizontal="center")

    def _fill_nf(pct, meta, atencao):
        # MENOR é melhor: verde ≤ meta, amarelo ≤ atenção, vermelho acima.
        if pct is None or meta is None or atencao is None:
            return None
        cor = "C6EFCE" if pct <= meta else ("FFEB9C" if pct <= atencao else "FFC7CE")
        return PatternFill(start_color=cor, end_color=cor, fill_type="solid")

    _situacao = {"verde": "No alvo", "amarelo": "Atenção", "vermelho": "Crítico", None: "—"}

    r = data["resumo"]
    mes = data.get("mes")
    wb = Workbook()

    # ── Aba 1: Resumo geral ─────────────────────────────────────────────
    ws = wb.active
    ws.title = "Resumo geral"
    _escopo_label = {"distribuicao": "Distribuição (venda)", "coleta": "Coleta (entrada)", "todas": "Todas"}
    escopo = data.get("escopo", "distribuicao")
    cr = data.get("coletas_resumo") or {}
    ws["A1"] = "Custo de Frete sobre Faturamento (%NF)"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}"
                + (f" — Mês {mes}" if mes else " — Todos os meses")
                + f" — Escopo: {_escopo_label.get(escopo, escopo)}")
    linhas_resumo = [
        ("%NF geral (frete ÷ valor NF)", r["pct_nf"], pct_fmt),
        ("Meta efetiva do mix (alvo ≤)", r["meta_efetiva"], pct_fmt),
        ("Atenção efetiva do mix (≤)", r["atencao_efetiva"], pct_fmt),
        ("Valor faturado transportado (NF)", r["valor_nf"], money_fmt),
        ("Frete sobre NF (base do %)", r["frete_base"], money_fmt),
        ("Frete total pago (todos os CTes)", r["frete_total"], money_fmt),
        ("Ticket médio de frete", r["ticket_frete"], money_fmt),
        ("CTes considerados", r["ctes"], None),
        ("CTes com valor de NF", r["com_valor_nf"], None),
        ("CTes sem valor de NF (fora do %)", r["sem_valor_nf"], None),
        ("Transportadoras", r["transportadoras"], None),
        ("No alvo", r["no_alvo"], None),
        ("Em atenção", r["em_atencao"], None),
        ("Críticas", r["criticas"], None),
        ("— Coletas de entrada (contadas à parte) —", None, None),
        ("Coletas: CTes", cr.get("ctes", 0), None),
        ("Coletas: frete total", cr.get("frete_total", 0), money_fmt),
        ("Coletas: valor NF", cr.get("valor_nf", 0), money_fmt),
        ("Coletas: %NF (frete ÷ NF)", cr.get("pct_nf"), pct_fmt),
    ]
    for i, (label, valor, fmt) in enumerate(linhas_resumo, start=4):
        ws[f"A{i}"] = label
        ws[f"A{i}"].font = Font(bold=True)
        cell = ws[f"B{i}"]
        cell.value = valor if valor is not None else "—"
        if fmt and isinstance(valor, (int, float)):
            cell.number_format = fmt
    fill = _fill_nf(r["pct_nf"], r["meta_efetiva"], r["atencao_efetiva"])
    if fill:
        ws["B4"].fill = fill
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 18

    # ── Aba 2: Por transportadora ───────────────────────────────────────
    ws2 = wb.create_sheet("Por transportadora")
    ws2["A1"] = "Custo de frete sobre faturamento por transportadora"
    ws2["A1"].font = Font(bold=True, size=13)
    headers = ["Transportadora", "%NF", "Meta mix", "Situação", "Valor NF", "Frete s/ NF",
               "Frete total", "Ticket médio", "CTes", "Sem valor NF"]
    _cabecalho(ws2, headers, 3)
    for row_idx, t in enumerate(data["por_transportadora"], start=4):
        vals = [
            t["transportadora"],
            t["pct_nf"] if t["pct_nf"] is not None else "—",
            t["meta_efetiva"] if t["meta_efetiva"] is not None else "—",
            _situacao.get(t["status"], "—"),
            t["valor_nf"], t["frete_base"], t["frete_total"], t["ticket_frete"],
            t["ctes"], t["sem_valor_nf"],
        ]
        for col_idx, val in enumerate(vals, start=1):
            c = ws2.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
            if col_idx in (2, 3) and isinstance(val, (int, float)):
                c.number_format = pct_fmt
            elif col_idx in (5, 6, 7, 8) and isinstance(val, (int, float)):
                c.number_format = money_fmt
        fill = _fill_nf(t["pct_nf"], t["meta_efetiva"], t["atencao_efetiva"])
        if fill:
            ws2.cell(row=row_idx, column=2).fill = fill
            ws2.cell(row=row_idx, column=4).fill = fill
    for col_idx, w in enumerate([26, 9, 9, 10, 16, 16, 16, 14, 8, 12], start=1):
        ws2.column_dimensions[get_column_letter(col_idx)].width = w
    ws2.freeze_panes = "A4"

    # ── Aba 3: Evolução mensal ──────────────────────────────────────────
    ws3 = wb.create_sheet("Evolução mensal")
    ws3["A1"] = "Evolução mensal do %NF (geral)"
    ws3["A1"].font = Font(bold=True, size=13)
    _cabecalho(ws3, ["Mês", "%NF", "Meta mix", "Valor NF", "Frete s/ NF", "Frete total", "CTes"], 3)
    for row_idx, s in enumerate(data["serie_mensal"], start=4):
        vals = [s["mes"], s["pct_nf"] if s["pct_nf"] is not None else "—",
                s["meta_efetiva"] if s["meta_efetiva"] is not None else "—",
                s["valor_nf"], s["frete_base"], s["frete_total"], s["ctes"]]
        for col_idx, val in enumerate(vals, start=1):
            c = ws3.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
            if col_idx in (2, 3) and isinstance(val, (int, float)):
                c.number_format = pct_fmt
            elif col_idx in (4, 5, 6) and isinstance(val, (int, float)):
                c.number_format = money_fmt
        fill = _fill_nf(s["pct_nf"], s["meta_efetiva"], s["atencao_efetiva"])
        if fill:
            ws3.cell(row=row_idx, column=2).fill = fill
    for col_idx, w in enumerate([12, 9, 9, 16, 16, 16, 8], start=1):
        ws3.column_dimensions[get_column_letter(col_idx)].width = w
    ws3.freeze_panes = "A4"

    # ── Aba 4: Por estado (UF) ──────────────────────────────────────────
    ws4 = wb.create_sheet("Por estado")
    ws4["A1"] = "Custo de frete sobre faturamento por estado (UF) de destino"
    ws4["A1"].font = Font(bold=True, size=13)
    _cabecalho(ws4, ["UF", "%NF", "Meta", "Atenção", "Situação", "Valor NF", "Frete s/ NF", "Frete total", "CTes"], 3)
    for row_idx, s in enumerate(data.get("por_estado", []), start=4):
        vals = [s["uf"], s["pct_nf"] if s["pct_nf"] is not None else "—",
                s.get("meta"), s.get("atencao"),
                _situacao.get(s["status"], "—"), s["valor_nf"], s["frete_base"],
                s["frete_total"], s["ctes"]]
        for col_idx, val in enumerate(vals, start=1):
            c = ws4.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
            if col_idx in (2, 3, 4) and isinstance(val, (int, float)):
                c.number_format = pct_fmt
            elif col_idx in (6, 7, 8) and isinstance(val, (int, float)):
                c.number_format = money_fmt
        fill = _fill_nf(s["pct_nf"], s.get("meta"), s.get("atencao"))
        if fill:
            ws4.cell(row=row_idx, column=2).fill = fill
            ws4.cell(row=row_idx, column=5).fill = fill
    for col_idx, w in enumerate([6, 9, 8, 9, 10, 16, 16, 16, 8], start=1):
        ws4.column_dimensions[get_column_letter(col_idx)].width = w
    ws4.freeze_panes = "A4"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
