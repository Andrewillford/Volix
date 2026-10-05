"""
Comparativo de tabelas de frete entre transportadoras.

Para um destino (CEP), resolve em cada transportadora a linha de preço aplicável
(mesma lógica de matching do motor de cotação) e devolve, lado a lado:
  • o valor do frete por faixa de peso (para achar a mais barata por faixa);
  • as generalidades (GRIS, TAS, pedágio, despacho, ad valorem, cubagem);
  • o prazo e, opcionalmente, o total simulado para um peso/valor.

Serve à análise de acordo (qual tabela é mais barata onde), dentro do Cadastro.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pandas as pd

from app.loaders import WEIGHT_BANDS
from app.rate_calculator import (
    _match_precos,
    _match_prazos_by_cep,
    _match_prazos_by_city,
    quote_for_carrier,
)

# Faixas excedentes na ordem de preferência (R$/kg acima da última faixa fixa).
_EXC_KEYS = ("exc_150", "exc_200", "ac200", "ac1000", "ac4000")

BANDA_LABEL = {
    "f10": "≤10", "f20": "≤20", "f30": "≤30", "f50": "≤50", "f60": "≤60", "f70": "≤70",
    "f100": "≤100", "f150": "≤150", "f200": "≤200",
}


def _num(v) -> Optional[float]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        f = float(v)
        return f if f != 0 else None
    except (ValueError, TypeError):
        return None


def comparar(cep: str, precos: Dict, prazos: Dict,
             peso: Optional[float] = None, valor: Optional[float] = None,
             city_uf: Optional[Tuple[str, str]] = None) -> Dict:
    cep = "".join(c for c in str(cep) if c.isdigit()).zfill(8)
    transportadoras: List[Dict] = []

    for carrier in sorted(set(precos) & set(prazos)):
        precos_data = precos[carrier]
        prazos_df = prazos[carrier]
        meta = precos_data.get("meta", {})

        prazo_row = _match_prazos_by_cep(prazos_df, cep)
        if prazo_row is None and city_uf:
            prazo_row = _match_prazos_by_city(prazos_df, city_uf[0], city_uf[1])
        if prazo_row is None:
            transportadoras.append({"nome": carrier, "cobertura": False,
                                    "motivo": "sem cobertura de prazo para o destino"})
            continue

        regiao = str(prazo_row.get("REGIAO", "")).strip()
        uf = str(prazo_row.get("UF", "")).strip()
        prazo_dias = int(prazo_row.get("PRAZO_DIAS", 0) or 0)

        price_row = _match_precos(precos_data, regiao, uf)
        if price_row is None:
            transportadoras.append({"nome": carrier, "cobertura": False,
                                    "motivo": f"sem tarifa para {uf}/{regiao}"})
            continue

        faixas = {}
        for bk, _ in WEIGHT_BANDS:
            v = _num(price_row.get(bk))
            if v is not None and v > 0:
                faixas[bk] = round(v, 2)

        excedente = None
        for k in _EXC_KEYS:
            v = _num(price_row.get(k))
            if v is not None and v > 0:
                excedente = round(v, 3)
                break

        pedagio = _num(price_row.get("pedagio_col")) or meta.get("pedagio_ref") or 0
        despacho_row = price_row.get("despacho")
        despacho = float(despacho_row) if despacho_row not in (None, "") and pd.notna(despacho_row) else meta.get("despacho", 0)
        advalorem = _num(price_row.get("advalorem_pct")) or _num(price_row.get("frete_val_pct")) or 0

        item = {
            "nome": carrier,
            "cobertura": True,
            "uf": uf,
            "regiao_casada": regiao,
            "prazo_dias": prazo_dias,
            "faixas": faixas,
            "excedente": excedente,
            "gris_pct": round(float(meta.get("gris_pct", 0)), 6),
            "gris_min": round(float(meta.get("gris_min", 0)), 2),
            "tec_pct": round(float(meta.get("tec_pct", 0) or 0), 6),
            "tas": round(float(meta.get("tas", 0)), 2),
            "pedagio": round(float(pedagio), 2),
            "despacho": round(float(despacho), 2),
            "advalorem_pct": round(float(advalorem), 6),
            "cubagem": float(meta.get("cubagem_fator", 0) or 0),
        }

        if peso:
            dm, _ = quote_for_carrier(carrier, precos_data, prazos_df, cep,
                                      float(peso), float(valor or 0), city_uf)
            item["total"] = dm.final_shipping_cost if dm else None
        transportadoras.append(item)

    cobertas = [t for t in transportadoras if t.get("cobertura")]
    bandas = [bk for bk, _ in WEIGHT_BANDS if any(bk in t["faixas"] for t in cobertas)]

    return {
        "cep": cep,
        "cidade_uf": list(city_uf) if city_uf else None,
        "peso": peso,
        "valor": valor,
        "bandas": bandas,
        "banda_labels": {bk: BANDA_LABEL.get(bk, bk) for bk in bandas},
        "transportadoras": transportadoras,
    }
