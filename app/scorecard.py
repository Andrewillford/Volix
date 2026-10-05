"""
Scorecard de Desempenho de Transportadoras.

Foco exclusivo em **desempenho de entrega** (OTD — On Time Delivery), a partir
da Torre de Controle (app/tracking.py). NÃO trata divergência de CTe/frete —
isso vive no painel de Auditoria.

Eficiência = OTD% (entregas no prazo ÷ entregas realizadas). Metas/sinalização:
  • OTD ≥ 95%  → verde   (no alvo)
  • 85–95%     → amarelo (atenção)
  • < 85%      → vermelho (crítico)

Entrega, por transportadora e geral: KPIs, distribuição por situação e série
mensal (para os gráficos interativos). Reaproveita os status já calculados por
tracking.query_entregas().
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app import tracking

# Limiares de eficiência (OTD%). Mesmos usados no Excel da torre. Servem de
# fallback quando a transportadora não tem meta própria na base de acordo.
META_OTD = 95.0
ATENCAO_OTD = 85.0


def _metas_transp() -> Dict[str, Dict[str, float]]:
    """Metas de OTD por transportadora cadastradas no painel /cadastro (base de
    acordo). Falha silenciosa → cai nos limiares globais."""
    try:
        from app import cadastro
        return cadastro.metas_otd()
    except Exception:
        return {}


def status_cor_meta(otd: Optional[float], meta: float, atencao: float) -> Optional[str]:
    if otd is None:
        return None
    if otd >= meta:
        return "verde"
    if otd >= atencao:
        return "amarelo"
    return "vermelho"

def status_cor(otd: Optional[float]) -> Optional[str]:
    """'verde' / 'amarelo' / 'vermelho' pela faixa de OTD; None se ainda não há
    entregas realizadas para avaliar."""
    if otd is None:
        return None
    if otd >= META_OTD:
        return "verde"
    if otd >= ATENCAO_OTD:
        return "amarelo"
    return "vermelho"


def _agrega(entregas: List[Dict]) -> Dict:
    """Consolida uma lista de entregas em métricas de desempenho.
    Usa a bucketização única de tracking (fonte de verdade do OTD)."""
    b = tracking.novo_bucket()
    # Faixas de severidade de atraso (só entregas REALIZADAS com atraso; cumulativas
    # e inclusivas: "1 dia ou mais" cobre todas as atrasadas, "2 dias ou mais" as com
    # 2+, etc. — assim o 1º nível bate com o total entregue com atraso).
    atraso1 = atraso2 = atraso3 = 0
    for e in entregas:
        tracking.classificar_entrega(b, e)
        if e.get("status") == "ENTREGUE_ATRASADO":
            dz = e.get("dias_atraso") or 0
            if dz >= 1:
                atraso1 += 1
            if dz >= 2:
                atraso2 += 1
            if dz >= 3:
                atraso3 += 1

    otd = tracking.otd_do_bucket(b)
    return {
        "entregas": b["total"],
        "entregues": b["entregues"],
        "avaliadas_otd": tracking.avaliadas_otd(b),
        "no_prazo": b["no_prazo"],
        "entregue_atrasado": b["entregue_atrasado"],
        "entregue_sem_prazo": b["entregue_sem_prazo"],
        "atrasadas_abertas": b["atrasadas_abertas"],
        "em_transito": b["em_transito"],
        "devolvidas": b["devolvidas"],
        "sem_prazo": b["sem_prazo"],
        "atraso_medio_dias": tracking.atraso_medio_do_bucket(b),
        "atraso_mais1": atraso1,
        "atraso_mais2": atraso2,
        "atraso_mais3": atraso3,
        "otd_pct": otd,
        "status": status_cor(otd),
    }


UFS = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG",
    "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
}


def _uf_de(e: Dict) -> Optional[str]:
    """UF de destino a partir do campo destino ('Município/UF')."""
    d = e.get("destino") or ""
    if "/" in d:
        uf = d.rsplit("/", 1)[-1].strip().upper()
        if uf in UFS:
            return uf
    return None


def _por_estado(entregas: List[Dict]) -> List[Dict]:
    """Desempenho (OTD) agregado por UF de destino, ordenado por UF."""
    por_uf: Dict[str, List[Dict]] = {}
    for e in entregas:
        uf = _uf_de(e)
        if uf:
            por_uf.setdefault(uf, []).append(e)
    linhas = []
    for uf in sorted(por_uf):
        ag = _agrega(por_uf[uf])
        ag["uf"] = uf
        linhas.append(ag)
    return linhas


def _mes_de(e: Dict) -> str:
    return (e.get("data_emissao") or "")[:7]


def _serie_mensal(entregas: List[Dict]) -> List[Dict]:
    """OTD% por mês (emissão), mais antigo → mais recente, para os gráficos de linha."""
    por_mes: Dict[str, List[Dict]] = {}
    for e in entregas:
        m = _mes_de(e)
        if m:
            por_mes.setdefault(m, []).append(e)
    serie = []
    for m in sorted(por_mes):
        ag = _agrega(por_mes[m])
        serie.append({
            "mes": m,
            "otd_pct": ag["otd_pct"],
            "entregas": ag["entregas"],
            "entregues": ag["entregues"],
        })
    return serie


def scorecard(mes: Optional[str] = None, uf: Optional[str] = None,
              transp: Optional[str] = None) -> Dict:
    """Desempenho de entrega consolidado: resumo geral + por transportadora
    (com série mensal em cada) + série mensal geral + por estado (UF).
    Filtros combináveis: `mes` ('YYYY-MM'); `transp` (transportadora exata, filtra
    tudo incl. o mapa); `uf` (uma UF ou lista por vírgula, o mapa segue mostrando
    todas as UFs para permitir trocar)."""
    entregas_mes = tracking.query_entregas(mes=mes, limit=100000)

    def _nome(e):
        return e["transportadora"] or "Desconhecida"

    # Lista de transportadoras do mês (para o dropdown), independente dos filtros.
    lista_transportadoras = sorted({_nome(e) for e in entregas_mes})

    # Filtro por transportadora (afeta tudo, inclusive o mapa por estado).
    transp = (transp or "").strip() or None
    def _por_transp(lst):
        return [e for e in lst if _nome(e) == transp] if transp else lst

    # Mapa por estado: mês + transportadora, mostrando todas as UFs (para trocar de UF).
    por_estado = _por_estado(_por_transp(entregas_mes))

    # `uf` pode ser uma UF ou uma lista separada por vírgula (seleção múltipla no mapa).
    ufs = {u.strip().upper() for u in (uf or "").split(",")}
    ufs = {u for u in ufs if u in UFS}
    entregas = _por_transp(entregas_mes)
    if ufs:
        entregas = [e for e in entregas if _uf_de(e) in ufs]

    geral = _agrega(entregas)

    por_transp_ent: Dict[str, List[Dict]] = {}
    for e in entregas:
        por_transp_ent.setdefault(e["transportadora"] or "Desconhecida", []).append(e)

    metas = _metas_transp()
    linhas = []
    for nome, ents in por_transp_ent.items():
        ag = _agrega(ents)
        ag["transportadora"] = nome
        ag["serie"] = _serie_mensal(ents)
        # Colorir OTD pela meta da transportadora (base de acordo), se cadastrada.
        m = metas.get(nome)
        ag["meta"] = m["meta"] if m else META_OTD
        ag["atencao"] = m["atencao"] if m else ATENCAO_OTD
        if m:
            ag["status"] = status_cor_meta(ag["otd_pct"], m["meta"], m["atencao"])
        linhas.append(ag)

    # Melhor OTD primeiro; sem OTD (nada entregue) vai ao fim.
    linhas.sort(key=lambda x: (x["otd_pct"] is None, -(x["otd_pct"] or 0), -x["entregas"]))

    avaliadas = [l for l in linhas if l["otd_pct"] is not None]
    resumo = dict(geral)
    resumo.update({
        "transportadoras": len(linhas),
        "no_alvo": sum(1 for l in avaliadas if l["status"] == "verde"),
        "em_atencao": sum(1 for l in avaliadas if l["status"] == "amarelo"),
        "criticas": sum(1 for l in avaliadas if l["status"] == "vermelho"),
    })

    return {
        "meta": META_OTD,
        "atencao": ATENCAO_OTD,
        "mes": mes,
        "uf": sorted(ufs),
        "transp": transp,
        "lista_transportadoras": lista_transportadoras,
        "resumo": resumo,
        "por_transportadora": linhas,
        "serie_mensal": _serie_mensal(entregas),
        "por_estado": por_estado,
    }


def list_meses() -> List[str]:
    """Meses com entregas registradas, mais recentes primeiro."""
    return tracking.list_meses()


# ─── Exportação Excel ────────────────────────────────────────────────────────

def export_xlsx(data: Dict) -> bytes:
    """Relatório de desempenho: Resumo geral + Desempenho por transportadora
    (OTD colorido pela meta) + Evolução mensal do OTD."""
    import io
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from app.history import _sanitize_cell

    header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    def _cabecalho(ws, headers, row):
        for col_idx, label in enumerate(headers, start=1):
            c = ws.cell(row=row, column=col_idx, value=label)
            c.fill = header_fill
            c.font = header_font
            c.alignment = Alignment(horizontal="center")

    def _fill_otd(otd):
        if otd is None:
            return None
        cor = "C6EFCE" if otd >= META_OTD else ("FFEB9C" if otd >= ATENCAO_OTD else "FFC7CE")
        return PatternFill(start_color=cor, end_color=cor, fill_type="solid")

    r = data["resumo"]
    mes = data.get("mes")

    wb = Workbook()

    # ── Aba 1: Resumo geral ─────────────────────────────────────────────
    ws = wb.active
    ws.title = "Resumo geral"
    ws["A1"] = "Scorecard de Desempenho de Transportadoras"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}" + (f" — Mês {mes}" if mes else " — Todos os meses")
    resumo = [
        ("OTD geral (base: entregas com prazo)", f"{r['otd_pct']}%" if r["otd_pct"] is not None else "—"),
        ("Meta de eficiência", f"{META_OTD:.0f}%"),
        ("Transportadoras avaliadas", r["transportadoras"]),
        ("No alvo (≥95%)", r["no_alvo"]),
        ("Em atenção (85–95%)", r["em_atencao"]),
        ("Críticas (<85%)", r["criticas"]),
        ("Total de entregas", r["entregas"]),
        ("Entregues", r["entregues"]),
        ("Entregas avaliadas no OTD", r.get("avaliadas_otd", 0)),
        ("Entregues no prazo", r["no_prazo"]),
        ("Entregues com atraso", r["entregue_atrasado"]),
        ("Entregues sem prazo (fora do OTD)", r.get("entregue_sem_prazo", 0)),
        ("Atrasadas em aberto", r["atrasadas_abertas"]),
        ("Em trânsito", r["em_transito"]),
        ("Devolvidas", r["devolvidas"]),
        ("Sem prazo de tabela (não entregues)", r["sem_prazo"]),
        ("Atraso médio (dias)", r["atraso_medio_dias"]),
        ("Atraso de 1 dia ou mais", r.get("atraso_mais1", 0)),
        ("Atraso de 2 dias ou mais", r.get("atraso_mais2", 0)),
        ("Atraso de 3 dias ou mais", r.get("atraso_mais3", 0)),
    ]
    for i, (label, valor) in enumerate(resumo, start=4):
        ws[f"A{i}"] = label
        ws[f"A{i}"].font = Font(bold=True)
        ws[f"B{i}"] = valor
    fill = _fill_otd(r["otd_pct"])
    if fill:
        ws["B4"].fill = fill
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 16

    # ── Aba 2: Desempenho por transportadora ────────────────────────────
    ws2 = wb.create_sheet("Por transportadora")
    ws2["A1"] = "Desempenho por transportadora"
    ws2["A1"].font = Font(bold=True, size=13)
    headers = ["Transportadora", "OTD %", "Situação", "Entregas", "Entregues",
               "No prazo", "Com atraso", "Atrasadas abertas", "Em trânsito",
               "Devolvidas", "Sem prazo", "Atraso médio (dias)"]
    _cabecalho(ws2, headers, 3)
    _situacao = {"verde": "No alvo", "amarelo": "Atenção", "vermelho": "Crítico", None: "—"}
    for row_idx, t in enumerate(data["por_transportadora"], start=4):
        vals = [
            t["transportadora"],
            t["otd_pct"] if t["otd_pct"] is not None else "—",
            _situacao.get(t["status"], "—"),
            t["entregas"], t["entregues"], t["no_prazo"], t["entregue_atrasado"],
            t["atrasadas_abertas"], t["em_transito"], t["devolvidas"], t["sem_prazo"],
            t["atraso_medio_dias"],
        ]
        for col_idx, val in enumerate(vals, start=1):
            ws2.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
        fill = _fill_otd(t["otd_pct"])
        if fill:
            ws2.cell(row=row_idx, column=2).fill = fill
            ws2.cell(row=row_idx, column=3).fill = fill
    for col_idx, w in enumerate([26, 8, 10, 10, 10, 9, 11, 17, 11, 11, 10, 17], start=1):
        ws2.column_dimensions[get_column_letter(col_idx)].width = w
    ws2.freeze_panes = "A4"

    # ── Aba 3: Evolução mensal do OTD ───────────────────────────────────
    ws3 = wb.create_sheet("Evolução mensal")
    ws3["A1"] = "Evolução mensal do OTD (geral)"
    ws3["A1"].font = Font(bold=True, size=13)
    _cabecalho(ws3, ["Mês", "OTD %", "Entregas", "Entregues"], 3)
    for row_idx, s in enumerate(data["serie_mensal"], start=4):
        vals = [s["mes"], s["otd_pct"] if s["otd_pct"] is not None else "—", s["entregas"], s["entregues"]]
        for col_idx, val in enumerate(vals, start=1):
            ws3.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
        fill = _fill_otd(s["otd_pct"])
        if fill:
            ws3.cell(row=row_idx, column=2).fill = fill
    for col_idx, w in enumerate([12, 8, 10, 10], start=1):
        ws3.column_dimensions[get_column_letter(col_idx)].width = w
    ws3.freeze_panes = "A4"

    # ── Aba 4: Eficiência por estado (UF) ───────────────────────────────
    ws4 = wb.create_sheet("Por estado")
    ws4["A1"] = "Eficiência por estado (UF) de destino"
    ws4["A1"].font = Font(bold=True, size=13)
    _cabecalho(ws4, ["UF", "OTD %", "Situação", "Entregas", "No prazo",
                     "Atrasadas abertas", "Em trânsito", "Devolvidas"], 3)
    for row_idx, s in enumerate(data.get("por_estado", []), start=4):
        vals = [s["uf"], s["otd_pct"] if s["otd_pct"] is not None else "—",
                _situacao.get(s["status"], "—"), s["entregas"], s["no_prazo"],
                s["atrasadas_abertas"], s["em_transito"], s["devolvidas"]]
        for col_idx, val in enumerate(vals, start=1):
            ws4.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
        fill = _fill_otd(s["otd_pct"])
        if fill:
            ws4.cell(row=row_idx, column=2).fill = fill
            ws4.cell(row=row_idx, column=3).fill = fill
    for col_idx, w in enumerate([6, 8, 10, 10, 9, 17, 11, 11], start=1):
        ws4.column_dimensions[get_column_letter(col_idx)].width = w
    ws4.freeze_panes = "A4"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
