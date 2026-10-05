"""
Relatório diário da Torre de Controle — análise por transportadora.

Classifica as entregas EM ABERTO em três alertas de prazo e junta o desempenho
(OTD, atraso médio…) de cada transportadora. É o "motor" do relatório automático
das 8h — independente do canal de envio (painel, e-mail, Telegram, WhatsApp).

Blocos por transportadora:
  🔴 vencidas       — atrasadas sem baixa (status ATRASADO)
  🟡 vence_hoje     — previsão para hoje (status VENCE_HOJE)
  🟠 proximas       — vencem nos próximos N dias úteis (padrão 2)
  📊 desempenho     — OTD %, atraso médio, em trânsito, entregues, devolvidas

`gerar()` devolve a estrutura de dados; `render_html()` e `render_texto()`
formatam para tela/e-mail e para mensagem curta (WhatsApp/Telegram).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from typing import Dict, List, Optional

from app import tracking
from app.feriados import somar_dias_uteis

# Categorias de ocorrência que sinalizam problema (base para "pontos de atenção").
_CAT_PROBLEMA = {
    "TENTATIVA_FRUSTRADA": ("🚫", "tentativas frustradas (recusa/ausente/fechado)"),
    "SINISTRO": ("📦", "sinistros (avaria/extravio/roubo/falta)"),
    "DEVOLUCAO": ("↩", "em devolução"),
}
_META_OTD = 95.0  # abaixo disso, a praça entra como ponto de atenção

# Status "em aberto" (ainda não finalizados) — os únicos que geram alerta.
_ABERTOS = {"EM_TRANSITO", "VENCE_HOJE", "ATRASADO", "SEM_PRAZO"}


def _uf(destino: Optional[str]) -> str:
    d = (destino or "").strip()
    u = d[-2:].upper()
    return u if len(u) == 2 and u.isalpha() else ""


def _item(e: Dict) -> Dict:
    """Resumo de uma entrega para as listas de alerta."""
    return {
        "numero_cte": e.get("numero_cte"),
        "numeros_nf": e.get("numeros_nf"),
        "destino": e.get("destino"),
        "uf": _uf(e.get("destino")),
        "data_prevista": e.get("data_prevista"),
        "dias_atraso": e.get("dias_atraso"),
        "motivo": e.get("problema_desc"),  # razão da tentativa frustrada (se houver)
    }


def gerar(dias_proximo: int = 2, hoje: Optional[date] = None) -> Dict:
    """Monta o relatório do dia. `dias_proximo`: janela (em dias úteis) do bloco
    'próximas de vencer'. Retorna estrutura pronta para renderizar em qualquer canal."""
    hoje = hoje or date.today()
    limite = somar_dias_uteis(hoje, dias_proximo).isoformat()

    entregas = tracking.query_entregas(limit=100000)
    k = tracking.kpis(entregas=entregas)
    perf = {t["transportadora"]: t for t in k["por_transportadora"]}

    por_transp: Dict[str, Dict] = {}

    def _bucket(nome: str) -> Dict:
        return por_transp.setdefault(nome, {"vencidas": [], "vence_hoje": [], "proximas": [], "problemas": []})

    for e in entregas:
        st = e.get("status")
        if st not in _ABERTOS:
            continue
        b = _bucket(e.get("transportadora") or "Desconhecida")
        if st == "ATRASADO":
            b["vencidas"].append(_item(e))
        elif st == "VENCE_HOJE":
            b["vence_hoje"].append(_item(e))
        elif st == "EM_TRANSITO" and e.get("data_prevista") and e["data_prevista"] <= limite:
            b["proximas"].append(_item(e))
        # "Com problema" é transversal (uma vencida pode também ter tentativa
        # frustrada): entra também no bloco de problemas.
        if e.get("problema"):
            b["problemas"].append(_item(e))

    # Garante que toda transportadora com desempenho apareça, mesmo sem alertas.
    for nome in perf:
        _bucket(nome)

    transportadoras = []
    for nome, b in por_transp.items():
        for lista in b.values():
            lista.sort(key=lambda x: (x["data_prevista"] or "9999", str(x["numero_cte"])))
        p = perf.get(nome, {})
        transportadoras.append({
            "transportadora": nome,
            "totais": {
                "vencidas": len(b["vencidas"]),
                "vence_hoje": len(b["vence_hoje"]),
                "proximas": len(b["proximas"]),
                "problemas": len(b["problemas"]),
            },
            "vencidas": b["vencidas"],
            "vence_hoje": b["vence_hoje"],
            "proximas": b["proximas"],
            "problemas": b["problemas"],
            "desempenho": {
                "otd_pct": p.get("otd_pct"),
                "atraso_medio_dias": p.get("atraso_medio_dias"),
                "em_transito": p.get("em_transito"),
                "entregues": p.get("entregues"),
                "devolvidas": p.get("devolvidas"),
                "total": p.get("total"),
            },
        })

    # Mais crítico primeiro: vencidas, depois vence hoje, depois próximas.
    transportadoras.sort(
        key=lambda t: (t["totais"]["vencidas"], t["totais"]["vence_hoje"], t["totais"]["proximas"]),
        reverse=True,
    )

    total_alertas = {
        "vencidas": sum(t["totais"]["vencidas"] for t in transportadoras),
        "vence_hoje": sum(t["totais"]["vence_hoje"] for t in transportadoras),
        "proximas": sum(t["totais"]["proximas"] for t in transportadoras),
        "problemas": sum(t["totais"]["problemas"] for t in transportadoras),
    }

    return {
        "data": hoje.isoformat(),
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "janela_dias": dias_proximo,
        "limite_proximo": limite,
        "geral": k["geral"],
        "total_alertas": total_alertas,
        "transportadoras": transportadoras,
    }


# ─── Renderizações ───────────────────────────────────────────────────────────

def _fmt_data(iso: Optional[str]) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso and len(iso) >= 10 else "—"


def _dm(iso: Optional[str]) -> str:
    """Data curta DD/MM (o ano vai no cabeçalho)."""
    return f"{iso[8:10]}/{iso[5:7]}" if iso and len(iso) >= 10 else "—"


# Largura útil dentro do bloco ``` do WhatsApp (fonte fixa): acima disso o app
# quebra a linha e desalinha a tabela. 32 caracteres cabem com folga no celular.
_MAXW = 32


def _limpa_motivo(m: str) -> str:
    """Tira o sufixo técnico '(via WebAPI …)' para o motivo caber no bloco."""
    return re.sub(r"\s*\(via WebAPI[^)]*\)", "", m or "").strip()


def _tag_item(it: Dict, modo: str) -> str:
    """Coluna curta à direita: atraso, 'hoje' ou data prevista (DD/MM)."""
    if modo == "vencida":
        return f"+{it['dias_atraso']}d" if it.get("dias_atraso") else "venc"
    if modo == "hoje":
        return "hoje"
    if modo == "proxima":
        return _dm(it.get("data_prevista"))
    return ""


def _linha_ident(it: Dict, tag: str) -> str:
    """1ª linha do item: 'CTe <n>  NF <n>' à esquerda e a tag (prazo) à direita,
    dentro de _MAXW. A NF vem no formato novo pedido pelo usuário."""
    ident = f"CTe {it.get('numero_cte') or '—'}"
    nf = it.get("numeros_nf")
    if nf:
        ident += f"  NF {nf}"
    if tag:
        pad = _MAXW - len(ident) - len(tag)
        if pad < 1:  # ident grande (muitas NFs): corta pra caber a tag
            ident = ident[: _MAXW - len(tag) - 1]
            pad = 1
        return ident + " " * pad + tag
    return ident[:_MAXW]


def _tabela_secao(itens: List[Dict], modo: str) -> List[str]:
    """Cada item em 2 linhas dentro do bloco monoespaçado: identidade
    (CTe + NF + prazo) em cima, destino embaixo. Em 'problema' entra ainda uma
    3ª linha com o motivo. Duas linhas curtas cabem em qualquer celular sem
    quebrar e mantêm CTe e NF visíveis."""
    linhas: List[str] = []
    for it in itens:
        linhas.append(_linha_ident(it, _tag_item(it, modo)))
        linhas.append((it.get("destino") or "—")[:_MAXW].rstrip())
        if modo == "problema":
            motivo = _limpa_motivo(it.get("motivo") or "")
            if motivo:
                linhas.append(motivo[:_MAXW].rstrip())
    return linhas


def _texto_transportadora(t: Dict, janela_dias: int) -> List[str]:
    """Bloco monoespaçado de UMA transportadora (desempenho + alertas)."""
    d = t["desempenho"]
    otd = "—" if d["otd_pct"] is None else f"{d['otd_pct']}%".replace(".", ",")
    L = [t["transportadora"].upper(),
         f"OTD {otd} · {d.get('em_transito') or 0} em transito"]
    secoes = (("PROBLEMA", "problemas", "problema"),
              ("VENCIDAS", "vencidas", "vencida"),
              ("VENCEM HOJE", "vence_hoje", "hoje"),
              (f"ATE {janela_dias} DIAS UTEIS", "proximas", "proxima"))
    tem = False
    for rot, chave, modo in secoes:
        itens = t.get(chave) or []
        if itens:
            tem = True
            L.append("")
            L.append(f"{rot} ({len(itens)})")
            L.extend(_tabela_secao(itens, modo))
    if not tem:
        L.append("OK - sem pendencias de prazo.")
    return L


def render_texto(rel: Dict, transportadora: Optional[str] = None) -> str:
    """Versão monoespaçada, envolta em bloco ``` do WhatsApp/Telegram: alinha em
    fonte fixa e não desenquadra ao colar. Dentro do bloco o app não renderiza
    *negrito*/emoji, então o texto é ASCII puro. Sem `transportadora`, traz o
    resumo geral + as que têm alerta; com, só o bloco daquela."""
    data = _fmt_data(rel["data"])
    titulo = f"RELATORIO ENTREGAS {data}"
    if transportadora:
        alvo = next((t for t in rel["transportadoras"] if t["transportadora"] == transportadora), None)
        if alvo is None:
            corpo = [titulo, f"Transportadora nao encontrada: {transportadora}"]
        else:
            corpo = [titulo, ""] + _texto_transportadora(alvo, rel["janela_dias"])
        return "```\n" + "\n".join(corpo) + "\n```"

    ta = rel["total_alertas"]
    L: List[str] = [
        titulo,
        f"{ta.get('problemas', 0)} prob · {ta['vencidas']} venc · {ta['vence_hoje']} hoje",
        f"{ta['proximas']} vencem em ate {rel['janela_dias']}d",
    ]
    for t in rel["transportadoras"]:
        tot = t["totais"]
        if not (tot["vencidas"] or tot["vence_hoje"] or tot["proximas"] or tot.get("problemas")):
            continue  # no resumo geral, só quem tem alerta
        L.append("")
        L.append("-" * 28)
        L.extend(_texto_transportadora(t, rel["janela_dias"]))
    return "```\n" + "\n".join(L) + "\n```"


def _tab_itens(itens: List[Dict]) -> str:
    if not itens:
        return '<div style="color:#6b7280;font-size:12px;">nenhuma</div>'
    linhas = "".join(
        f"<tr><td>{it['numero_cte'] or '—'}</td><td>{it.get('numeros_nf') or '—'}</td>"
        f"<td>{it.get('destino') or '—'}</td><td>{_fmt_data(it.get('data_prevista'))}</td>"
        f"<td style='text-align:right;'>{('+' + str(it['dias_atraso']) + 'd') if it.get('dias_atraso') else '—'}</td></tr>"
        for it in itens
    )
    return (
        "<table style='border-collapse:collapse;width:100%;font-size:12px;margin:4px 0 10px;'>"
        "<tr style='color:#6b7280;text-align:left;'><th>CTe</th><th>NF</th><th>Destino</th><th>Prevista</th>"
        "<th style='text-align:right;'>Atraso</th></tr>" + linhas + "</table>"
    )


def _tab_problemas(itens: List[Dict]) -> str:
    if not itens:
        return '<div style="color:#6b7280;font-size:12px;">nenhuma</div>'
    linhas = "".join(
        f"<tr><td>{it['numero_cte'] or '—'}</td><td>{it.get('numeros_nf') or '—'}</td>"
        f"<td>{it.get('destino') or '—'}</td><td><b>{it.get('motivo') or '—'}</b></td></tr>"
        for it in itens
    )
    return (
        "<table style='border-collapse:collapse;width:100%;font-size:12px;margin:4px 0 10px;'>"
        "<tr style='color:#6b7280;text-align:left;'><th>CTe</th><th>NF</th><th>Destino</th><th>Motivo</th></tr>"
        + linhas + "</table>"
    )


def render_html(rel: Dict) -> str:
    """HTML para painel/e-mail (autocontido, sem dependências)."""
    ta = rel["total_alertas"]
    blocos = []
    for t in rel["transportadoras"]:
        d = t["desempenho"]
        otd = "—" if d["otd_pct"] is None else f"{d['otd_pct']}%"
        tot = t["totais"]
        resumo = (
            f"<span style='color:#ea580c;'>⚠️ {tot.get('problemas', 0)} com problema</span> · "
            f"<span style='color:#dc2626;'>🔴 {tot['vencidas']} vencidas</span> · "
            f"<span style='color:#d97706;'>🟡 {tot['vence_hoje']} vencem hoje</span> · "
            f"<span style='color:#ea580c;'>🟠 {tot['proximas']} próximas</span>"
        )
        prob_html = (
            f"<div style='font-weight:600;font-size:12px;color:#ea580c;'>⚠️ Entregas com problema (tentativa frustrada)</div>{_tab_problemas(t.get('problemas') or [])}"
            if t.get("problemas") else ""
        )
        nome_attr = t["transportadora"].replace("'", "&#39;").replace('"', "&quot;")
        blocos.append(
            f"<div style='border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px;margin:10px 0;'>"
            f"<div style='display:flex;align-items:center;justify-content:space-between;gap:8px;'>"
            f"<div style='font-weight:700;font-size:15px;'>{t['transportadora']}</div>"
            f"<button type='button' data-copy-transp=\"{nome_attr}\" "
            f"style='background:#111827;color:#fff;border:none;border-radius:6px;padding:5px 10px;"
            f"font-size:12px;cursor:pointer;white-space:nowrap;'>📋 Copiar desta transp.</button>"
            f"</div>"
            f"<div style='color:#374151;font-size:12px;margin:2px 0 8px;'>"
            f"OTD <b>{otd}</b> · atraso médio {d.get('atraso_medio_dias') or 0}d · "
            f"{d.get('em_transito') or 0} em trânsito · {d.get('entregues') or 0} entregues · "
            f"{d.get('devolvidas') or 0} devolvidas</div>"
            f"<div style='font-size:12px;margin-bottom:6px;'>{resumo}</div>"
            f"{prob_html}"
            f"<div style='font-weight:600;font-size:12px;color:#dc2626;'>Vencidas</div>{_tab_itens(t['vencidas'])}"
            f"<div style='font-weight:600;font-size:12px;color:#d97706;'>Vencem hoje</div>{_tab_itens(t['vence_hoje'])}"
            f"<div style='font-weight:600;font-size:12px;color:#ea580c;'>Vencem em até {rel['janela_dias']} dias úteis</div>{_tab_itens(t['proximas'])}"
            f"</div>"
        )
    g = rel["geral"]
    otd_geral = "—" if g.get("otd_pct") is None else f"{g['otd_pct']}%"
    return (
        f"<div style='font-family:system-ui,Segoe UI,Arial,sans-serif;color:#111827;max-width:900px;'>"
        f"<h2 style='margin:0 0 2px;'>📦 Relatório de Entregas — {_fmt_data(rel['data'])}</h2>"
        f"<div style='color:#6b7280;font-size:12px;'>Gerado em {rel['gerado_em'].replace('T', ' ')} · "
        f"OTD geral {otd_geral}</div>"
        f"<div style='background:#f9fafb;border-radius:8px;padding:10px 14px;margin:10px 0;font-size:13px;'>"
        f"<b style='color:#ea580c;'>⚠️ {ta.get('problemas', 0)}</b> com problema · "
        f"<b style='color:#dc2626;'>🔴 {ta['vencidas']}</b> vencidas · "
        f"<b style='color:#d97706;'>🟡 {ta['vence_hoje']}</b> vencem hoje · "
        f"<b style='color:#ea580c;'>🟠 {ta['proximas']}</b> vencem em até {rel['janela_dias']} dias úteis</div>"
        + "".join(blocos) + "</div>"
    )


# ─── Análise detalhada por transportadora (desempenho por estado + atenção) ───

def _pontos_atencao(geral: Dict, estados: List[Dict], cat_count: Counter,
                    cat_top_uf: Dict[str, Counter]) -> List[str]:
    """Gera bullets de 'o que prestar atenção' a partir dos números da transportadora."""
    pts: List[str] = []
    # Piores praças (OTD abaixo da meta, com volume mínimo para ser significativo).
    criticos = [e for e in estados if e["otd_pct"] is not None and e["otd_pct"] < _META_OTD and e["entregas"] >= 3]
    criticos.sort(key=lambda e: (e["otd_pct"], -e["entregas"]))
    for e in criticos[:3]:
        pts.append(f"🔴 {e['uf']}: OTD {e['otd_pct']}% em {e['entregas']} entregas — praça com dificuldade.")
    # Estados com entregas atrasadas em aberto (ação imediata).
    abertos = [e for e in estados if e.get("atrasadas_abertas")]
    abertos.sort(key=lambda e: -e["atrasadas_abertas"])
    for e in abertos[:3]:
        pts.append(f"⏰ {e['uf']}: {e['atrasadas_abertas']} atrasada(s) em aberto.")
    # Ocorrências problemáticas (recusa, sinistro, devolução).
    for cat, (emo, rot) in _CAT_PROBLEMA.items():
        n = cat_count.get(cat, 0)
        if n:
            top = cat_top_uf.get(cat, Counter()).most_common(2)
            onde = ", ".join(f"{uf} ({c})" for uf, c in top if uf) if top else ""
            pts.append(f"{emo} {n} {rot}{(' — ' + onde) if onde else ''}.")
    # Atraso médio relevante.
    am = geral.get("atraso_medio_dias") or 0
    if am >= 2:
        pts.append(f"🐢 Atraso médio de {am} dias nas entregas que atrasaram.")
    if not pts:
        pts.append("✅ Sem pontos críticos no momento.")
    return pts


def analise_detalhada() -> Dict:
    """Análise por transportadora: desempenho por estado (UF), ocorrências
    problemáticas e pontos de atenção. Reaproveita a agregação do scorecard."""
    from app import scorecard as _sc

    entregas = tracking.query_entregas(limit=100000)
    ocs_por_chave = tracking.ocorrencias_por_chave()

    por_transp: Dict[str, List[Dict]] = {}
    for e in entregas:
        por_transp.setdefault(e.get("transportadora") or "Desconhecida", []).append(e)

    transportadoras = []
    for nome, ents in por_transp.items():
        geral = _sc._agrega(ents)

        # Desempenho por estado (UF), pior OTD primeiro (nulos por último).
        por_uf: Dict[str, List[Dict]] = {}
        uf_por_chave: Dict[str, Optional[str]] = {}
        for e in ents:
            uf = _sc._uf_de(e)
            uf_por_chave[e["chave_cte"]] = uf
            if uf:
                por_uf.setdefault(uf, []).append(e)
        estados = []
        for uf, lst in por_uf.items():
            ag = _sc._agrega(lst)
            estados.append({
                "uf": uf, "entregas": ag["entregas"], "otd_pct": ag["otd_pct"],
                "atraso_medio_dias": ag["atraso_medio_dias"],
                "atrasadas_abertas": ag["atrasadas_abertas"],
                "em_transito": ag["em_transito"], "status": ag["status"],
            })
        estados.sort(key=lambda x: (x["otd_pct"] is None, x["otd_pct"] if x["otd_pct"] is not None else 999, -x["entregas"]))

        # Ocorrências problemáticas por categoria (+ onde mais acontecem).
        cat_count: Counter = Counter()
        cat_top_uf: Dict[str, Counter] = {}
        for e in ents:
            uf = uf_por_chave.get(e["chave_cte"])
            for oc in ocs_por_chave.get(e["chave_cte"], []):
                cat = oc.get("categoria")
                if cat in _CAT_PROBLEMA:
                    cat_count[cat] += 1
                    cat_top_uf.setdefault(cat, Counter())[uf or "?"] += 1

        transportadoras.append({
            "transportadora": nome,
            "desempenho": {
                "otd_pct": geral["otd_pct"], "atraso_medio_dias": geral["atraso_medio_dias"],
                "entregas": geral["entregas"], "entregues": geral["entregues"],
                "em_transito": geral["em_transito"], "atrasadas_abertas": geral["atrasadas_abertas"],
                "devolvidas": geral["devolvidas"],
            },
            "estados": estados,
            "pontos_atencao": _pontos_atencao(geral, estados, cat_count, cat_top_uf),
        })

    # Mais crítica primeiro: pior OTD, depois mais atrasadas em aberto.
    transportadoras.sort(key=lambda t: (
        t["desempenho"]["otd_pct"] is None,
        t["desempenho"]["otd_pct"] if t["desempenho"]["otd_pct"] is not None else 999,
        -t["desempenho"]["atrasadas_abertas"],
    ))

    return {
        "data": date.today().isoformat(),
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "transportadoras": transportadoras,
    }


def _cor_otd(otd: Optional[float]) -> str:
    if otd is None:
        return "#6b7280"
    return "#16a34a" if otd >= 95 else ("#d97706" if otd >= 85 else "#dc2626")


def render_analise_html(an: Dict) -> str:
    """HTML da análise detalhada (painel/e-mail)."""
    blocos = []
    for t in an["transportadoras"]:
        d = t["desempenho"]
        otd = "—" if d["otd_pct"] is None else f"{d['otd_pct']}%"
        pts = "".join(f"<li style='margin:2px 0;'>{p}</li>" for p in t["pontos_atencao"])
        linhas = "".join(
            f"<tr><td><b>{e['uf']}</b></td><td style='text-align:right;'>{e['entregas']}</td>"
            f"<td style='text-align:right;color:{_cor_otd(e['otd_pct'])};font-weight:600;'>"
            f"{('—' if e['otd_pct'] is None else str(e['otd_pct']) + '%')}</td>"
            f"<td style='text-align:right;'>{e['atraso_medio_dias'] or 0}d</td>"
            f"<td style='text-align:right;'>{e['atrasadas_abertas'] or '—'}</td>"
            f"<td style='text-align:right;'>{e['em_transito'] or '—'}</td></tr>"
            for e in t["estados"]
        ) or "<tr><td colspan='6' style='color:#6b7280;'>Sem destino com UF identificada.</td></tr>"
        blocos.append(
            f"<div style='border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px;margin:10px 0;'>"
            f"<div style='font-weight:700;font-size:15px;'>{t['transportadora']} "
            f"<span style='font-weight:600;font-size:12px;color:{_cor_otd(d['otd_pct'])};'>OTD {otd}</span></div>"
            f"<div style='color:#374151;font-size:12px;margin:2px 0 8px;'>"
            f"{d['entregas']} entregas · {d.get('em_transito') or 0} em trânsito · "
            f"{d.get('atrasadas_abertas') or 0} atrasadas em aberto · atraso médio {d.get('atraso_medio_dias') or 0}d</div>"
            f"<div style='font-weight:600;font-size:12px;margin-bottom:2px;'>⚠️ Pontos de atenção</div>"
            f"<ul style='margin:0 0 10px;padding-left:18px;font-size:12px;'>{pts}</ul>"
            f"<div style='font-weight:600;font-size:12px;margin-bottom:2px;'>📍 Desempenho por estado (pior primeiro)</div>"
            f"<table style='border-collapse:collapse;width:100%;font-size:12px;'>"
            f"<tr style='color:#6b7280;text-align:right;'><th style='text-align:left;'>UF</th><th>Entregas</th>"
            f"<th>OTD</th><th>Atraso méd.</th><th>Atras. abertas</th><th>Em trânsito</th></tr>"
            f"{linhas}</table></div>"
        )
    return (
        f"<div style='font-family:system-ui,Segoe UI,Arial,sans-serif;color:#111827;max-width:900px;'>"
        f"<h2 style='margin:0 0 2px;'>📊 Análise por Transportadora</h2>"
        f"<div style='color:#6b7280;font-size:12px;margin-bottom:6px;'>Gerado em {an['gerado_em'].replace('T', ' ')} · "
        f"transportadora com pior OTD primeiro</div>"
        + "".join(blocos) + "</div>"
    )
