"""
Home / Painel Executivo — página inicial consolidada.

Não captura nada de novo: apenas AGREGA os KPIs de topo que cada painel já
calcula (auditoria, entregas/OTD, %NF, conferência de fatura, romaneio), para
uma visão única com cards clicáveis que levam ao painel de origem.

Tudo é derivado das mesmas funções que os painéis usam — a fonte de verdade
continua sendo cada módulo. Assim o Home nunca fica "fora de sincronia".
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app import history as audit_history
from app import tracking
from app import percent_nf as percent_nf_mod
from app import scorecard as scorecard_mod
from app import conferencia as conferencia_mod
from app import romaneio as romaneio_mod
from app import vendas as vendas_mod


def _num(v, default=0):
    return v if v is not None else default


def _financeiro(mes: Optional[str]) -> Dict:
    """Auditoria de CTe + %NF — o bloco que a diretoria olha primeiro."""
    ind = audit_history.indicadores(mes)["kpis"]
    nf = percent_nf_mod.percent_nf(mes)["resumo"]
    return {
        "total_ctes": _num(ind.get("total_ctes")),
        "ctes_divergente": _num(ind.get("ctes_divergente")),
        "pct_divergente": _num(ind.get("pct_divergente")),
        "valor_cobrado_total": _num(ind.get("valor_cobrado_total"), 0.0),
        "valor_divergencia_total": _num(ind.get("valor_divergencia_total"), 0.0),
        "pct_nf": nf.get("pct_nf"),
        "frete_total": _num(nf.get("frete_total"), 0.0),
        "valor_nf": _num(nf.get("valor_nf"), 0.0),
        "nf_meta": nf.get("meta_efetiva"),
        "nf_atencao": nf.get("atencao_efetiva"),
        "nf_status": nf.get("status"),
    }


def _entregas(mes: Optional[str]) -> Dict:
    """Torre de Controle + Scorecard — OTD e o que está em aberto/atrasado."""
    k = tracking.kpis(mes)
    g = k["geral"]
    return {
        "total": _num(g.get("total")),
        "otd_pct": g.get("otd_pct"),
        "em_transito": _num(g.get("em_transito")),
        "atrasadas_abertas": _num(g.get("atrasadas_abertas")),
        "devolvidas": _num(g.get("devolvidas")),
        "entregues": _num(g.get("entregues")),
        "ocorrencias_sem_vinculo": _num(k.get("ocorrencias_sem_vinculo")),
        "meta": scorecard_mod.META_OTD,
        "atencao": scorecard_mod.ATENCAO_OTD,
    }


def _conferencia() -> Dict:
    """Conferência de Fatura — totais financeiros vivos (recalculados).

    Não filtra por mês: o valor liberado/contestável é um saldo em aberto que a
    diretoria quer ver por inteiro, independente da competência da fatura."""
    faturas = conferencia_mod.list_faturas()
    total = liberado = em_revisao = contestavel = 0.0
    por_status = {k: 0 for k in conferencia_mod.STATUS_FATURA}
    for f in faturas:
        r = f.get("resumo") or {}
        total += _num(r.get("total_fatura"), 0.0)
        liberado += _num(r.get("valor_liberado"), 0.0)
        em_revisao += _num(r.get("valor_em_revisao"), 0.0)
        contestavel += _num(r.get("valor_contestavel"), 0.0)
        por_status[f.get("status")] = por_status.get(f.get("status"), 0) + 1
    return {
        "faturas": len(faturas),
        "total_fatura": round(total, 2),
        "valor_liberado": round(liberado, 2),
        "valor_em_revisao": round(em_revisao, 2),
        "valor_contestavel": round(contestavel, 2),
        "por_status": por_status,
    }


def _expedicao(mes: Optional[str]) -> Dict:
    """Romaneio de Expedição — romaneios do período (por situação) + fila."""
    romaneios = romaneio_mod.list_romaneios(mes=mes)
    por_situacao: Dict[str, int] = {}
    nfs_expedidas = 0
    for r in romaneios:
        por_situacao[r.get("situacao")] = por_situacao.get(r.get("situacao"), 0) + 1
        nfs_expedidas += _num((r.get("totais") or {}).get("qtd_nfs"))
    pool = romaneio_mod.list_pool()
    return {
        "romaneios": len(romaneios),
        "por_situacao": por_situacao,
        "nfs_expedidas": nfs_expedidas,
        "nfs_na_fila": _num((pool.get("totais") or {}).get("qtd_nfs")),
        "grupos_na_fila": len(pool.get("grupos") or []),
    }


def _comercial(mes: Optional[str]) -> Dict:
    """Análise de Vendas (Olist) — faturamento do período e canal líder.

    Lê do cache local de pedidos (não bate na API do Olist aqui). Se o cache
    ainda estiver vazio, retorna zeros — o card degrada limpo."""
    try:
        r = vendas_mod.resumo(mes)
    except Exception:
        return {"total_pedidos": 0, "faturados": 0, "faturamento_total": 0.0, "ticket_medio": 0.0,
                "cancelados": 0, "aguardando": 0, "canal_lider": None, "canal_lider_fat": 0.0,
                "canal_lider_part": 0.0, "num_canais": 0}
    k = r["kpis"]
    canais = [c for c in r.get("por_canal", []) if c.get("faturamento", 0) > 0]
    lider = canais[0] if canais else None  # por_canal já vem ordenado por faturamento desc
    return {
        "total_pedidos": _num(k.get("total_pedidos")),
        "faturados": _num(k.get("faturados")),
        "faturamento_total": _num(k.get("faturamento_total"), 0.0),
        "faturamento_liquido": _num(k.get("faturamento_liquido"), 0.0),
        "devolucoes": _num(k.get("devolucoes"), 0.0),
        "ticket_medio": _num(k.get("ticket_medio"), 0.0),
        "cancelados": _num(k.get("cancelados")),
        "aguardando": _num(k.get("aguardando")),
        "canal_lider": lider["canal"] if lider else None,
        "canal_lider_fat": _num(lider.get("faturamento"), 0.0) if lider else 0.0,
        "canal_lider_part": _num(lider.get("participacao"), 0.0) if lider else 0.0,
        "num_canais": len(canais),
    }


def _evolucao() -> Dict:
    """Séries mensais (sempre completas, ignoram o filtro de mês) para os
    sparklines de OTD e %NF no topo do Home."""
    otd = [
        {"mes": s["mes"], "valor": s["otd_pct"]}
        for s in scorecard_mod.scorecard(None)["serie_mensal"]
    ]
    nf = [
        {"mes": s["mes"], "valor": s["pct_nf"]}
        for s in percent_nf_mod.percent_nf(None)["serie_mensal"]
    ]
    return {"otd": otd, "nf": nf}


def resumo(mes: Optional[str] = None) -> Dict:
    """Consolidado do Home. `mes` no formato 'YYYY-MM' (None = tudo)."""
    return {
        "mes": mes,
        "financeiro": _financeiro(mes),
        "entregas": _entregas(mes),
        "conferencia": _conferencia(),
        "expedicao": _expedicao(mes),
        "comercial": _comercial(mes),
        "evolucao": _evolucao(),
    }


def list_meses() -> List[str]:
    """União dos meses conhecidos por auditoria e por entregas (mais recentes
    primeiro), para o seletor de mês do Home."""
    meses = set(audit_history.list_meses()) | set(tracking.list_meses())
    try:
        meses |= set(vendas_mod.list_meses())
    except Exception:
        pass
    return sorted(meses, reverse=True)
