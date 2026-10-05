# -*- coding: utf-8 -*-
"""Simulador de reajuste de tabela (#4).

Reprocessa CTes históricos de uma transportadora contra a mesma tabela com um
percentual de reajuste aplicado (frete-peso e taxas fixas em R$) e mede o impacto
em R$ — total e por UF. Usa a MESMA engine da auditoria (rate_calculator), então
o delta reflete exatamente a régua contratada. Arma de negociação: saber quanto um
reajuste proposto custaria sobre o volume real antes de assinar.
"""
import re
from typing import Dict, List, Optional

import pandas as pd

from app.rate_calculator import quote_for_carrier

# Campos de meta em R$ que escalam com o reajuste (percentuais como gris_pct,
# advalorem_pct, tec_pct NÃO escalam — são % da base, não valores de tarifa).
_META_REAJUSTAVEL = ("despacho", "pedagio_ref", "tas", "gris_min")
# Colunas de linha em R$ que escalam (fora as faixas de peso, tratadas à parte).
_COLS_REAJUSTAVEL = ("pedagio_col", "sec_cat", "despacho")


def _city_uf(destino: Optional[str]):
    """'São Paulo/SP' -> ('SÃO PAULO', 'SP'). None se não casar."""
    if not destino:
        return None
    m = re.match(r"^(.*)/([A-Za-z]{2})\s*$", str(destino).strip())
    if m:
        return (m.group(1).strip().upper(), m.group(2).upper())
    return None


def tabela_reajustada(precos_data: Dict, pct: float) -> Dict:
    """Cópia da tabela com o reajuste linear de `pct`% nas faixas de peso e nas
    taxas fixas em R$. Percentuais (ad valorem, GRIS%, TEC%) ficam intactos."""
    fator = 1.0 + (pct / 100.0)
    novo = dict(precos_data)
    df = precos_data["df"].copy()
    for bk in precos_data.get("band_keys", []):
        if bk in df.columns:
            df[bk] = pd.to_numeric(df[bk], errors="coerce") * fator
    for col in _COLS_REAJUSTAVEL:
        if col in df.columns:
            df[col] = df[col].apply(lambda v: (v * fator) if (v is not None and pd.notna(v)) else v)
    novo["df"] = df
    meta = dict(precos_data["meta"])
    for k in _META_REAJUSTAVEL:
        meta[k] = float(meta.get(k) or 0) * fator
    novo["meta"] = meta
    return novo


def simular(carrier_name: str, precos_data: Dict, prazos_df, historico: List[Dict],
            pct: float) -> Dict:
    """Re-cota cada CTe do histórico contra a tabela atual e a reajustada, e soma o
    impacto. Ambos os lados usam a mesma engine (e ignoram taxas por CEP igualmente),
    então o delta isola o efeito do reajuste."""
    tab_mod = tabela_reajustada(precos_data, pct)
    atual = simulado = 0.0
    n = sem_cobertura = 0
    por_uf: Dict[str, Dict] = {}
    for r in historico:
        peso = r.get("peso_kg")
        if not peso:
            continue
        cu = _city_uf(r.get("destino"))
        val = r.get("valor_mercadoria") or 0.0
        dm_a, _ = quote_for_carrier(carrier_name, precos_data, prazos_df, "", peso, val, cu)
        dm_b, _ = quote_for_carrier(carrier_name, tab_mod, prazos_df, "", peso, val, cu)
        if dm_a is None or dm_b is None:
            sem_cobertura += 1
            continue
        n += 1
        atual += dm_a.final_shipping_cost
        simulado += dm_b.final_shipping_cost
        uf = cu[1] if cu else "?"
        acc = por_uf.setdefault(uf, {"atual": 0.0, "sim": 0.0, "n": 0})
        acc["atual"] += dm_a.final_shipping_cost
        acc["sim"] += dm_b.final_shipping_cost
        acc["n"] += 1
    delta = simulado - atual
    return {
        "pct": pct,
        "ctes": n,
        "sem_cobertura": sem_cobertura,
        "custo_atual": round(atual, 2),
        "custo_simulado": round(simulado, 2),
        "delta": round(delta, 2),
        "delta_pct": round((delta / atual * 100), 2) if atual else None,
        "por_uf": sorted(
            [{"uf": k, "atual": round(v["atual"], 2), "simulado": round(v["sim"], 2),
              "delta": round(v["sim"] - v["atual"], 2), "ctes": v["n"]}
             for k, v in por_uf.items()],
            key=lambda x: -x["delta"]),
    }
