"""
Motor de cálculo de frete com matching inteligente de REGIAO.

Estratégia de matching (em ordem):
 1. Exact match UF + REGIAO
 2. Case-insensitive após normalização
 3. Extração de sufixo (strip UF prefix "SP - POLO" → "POLO")
 4. Mobile: strip 2 chars UF do código ("ES040" → "040")
 5. Posicional: "CAP/POLO/1" → 1ª linha UF; "INT 1/I/2" → 2ª; "INT 2/I2/3" → 3ª
 6. Fallback: 1ª linha disponível para o UF
"""
from __future__ import annotations

import re, math, logging
from datetime import date
from typing import Dict, Optional, Tuple

import pandas as pd

from app.models import DeliveryMethod, CarrierInfo
from app.loaders import WEIGHT_BANDS
from app.feriados import somar_dias_uteis

logger = logging.getLogger(__name__)

_VALID_UF = {
    "AC","AL","AM","AP","BA","CE","DF","ES","GO","MA","MG","MS","MT","PA",
    "PB","PE","PI","PR","RJ","RN","RO","RR","RS","SC","SE","SP","TO",
}

# ─── ICMS sobre o frete (embutido/"por dentro"), por UF de destino ────────────
# As transportadoras coletam na Grande Vitória (ES). Para origem ES: frete interno
# (ES→ES) usa a alíquota interna do ES; interestadual usa 12%. Editável por UF.
ICMS_ALIQ_POR_UF_DESTINO = {
    "ES": 0.17,  # operação interna no ES
    # demais UFs: interestadual padrão (ICMS_DEFAULT) — inclua aqui exceções se houver
}
ICMS_INTERESTADUAL_PADRAO = 0.12


def icms_aliquota(dest_uf: str, origem_uf: str = "ES") -> float:
    """Alíquota de ICMS sobre o frete para (origem_uf → dest_uf). Origem ES é o
    caso real das transportadoras; para outras origens aproxima interno=17%/inter=12%."""
    d = (dest_uf or "").upper()
    o = (origem_uf or "ES").upper()
    if d not in _VALID_UF:
        return 0.0
    if o == "ES":
        return ICMS_ALIQ_POR_UF_DESTINO.get(d, ICMS_INTERESTADUAL_PADRAO)
    return 0.17 if o == d else ICMS_INTERESTADUAL_PADRAO


def _uf_from_origin(origin: str) -> str:
    """Extrai a UF do texto de origem (ex.: 'Grande Vitória-ES' → 'ES'). Default ES."""
    m = re.search(r"([A-Za-z]{2})\s*$", (origin or "").strip())
    uf = m.group(1).upper() if m else "ES"
    return uf if uf in _VALID_UF else "ES"


def _add_business_days(start: date, days: int) -> date:
    """Avança `days` dias úteis pulando fins de semana e feriados nacionais
    (ver app.feriados). Ponto único de cálculo de prazo: auditoria (data
    prevista), cotação de frete (data estimada) e, por consequência, o OTD."""
    return somar_dias_uteis(start, days)


def _norm(s: str) -> str:
    """Normaliza: minúsculas, sem acento, sem caracteres especiais."""
    import unicodedata
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _get_frete_peso(price_row: pd.Series, weight: float) -> Optional[float]:
    """Seleciona preço da faixa de peso correta."""
    for band_key, limit in WEIGHT_BANDS:
        if band_key in price_row.index:
            val = price_row[band_key]
            if pd.notna(val) and float(val) > 0 and weight <= limit:
                return float(val)

    # Excedente: usar maior faixa fixa disponível + per-kg
    best_price, best_limit = None, 0.0
    for band_key, limit in reversed(WEIGHT_BANDS):
        if band_key in price_row.index:
            val = price_row[band_key]
            if pd.notna(val) and float(val) > 0:
                best_price, best_limit = float(val), limit
                break

    for exc_key in ("exc_150", "exc_200", "ac200", "ac1000", "ac4000"):
        if exc_key in price_row.index:
            val = price_row[exc_key]
            if pd.notna(val) and float(val) > 0:
                if best_price is not None:
                    return best_price + max(0, weight - best_limit) * float(val)
                return weight * float(val)

    return None


def _match_prazos_by_cep(df: pd.DataFrame, dest_cep: str) -> Optional[pd.Series]:
    rows = df[df["has_cep"]]
    mask = (rows["CEP_INICIAL"] <= dest_cep) & (rows["CEP_FINAL"] >= dest_cep)
    m = rows[mask]
    return m.iloc[0] if not m.empty else None


def _match_prazos_by_city(df: pd.DataFrame, city: str, uf: str) -> Optional[pd.Series]:
    # Casa por nome normalizado (sem acento/caixa) — o CEP resolvido vem com acento
    # (ex.: "São Paulo") e a tabela pode estar sem (ex.: "SAO PAULO"); comparar só
    # com .upper() deixava de casar transportadoras cujos prazos não têm CEP (ex.: Mobile).
    cn = _norm(city)
    un = str(uf).upper().strip()
    cidade_norm = df["CIDADE"].map(lambda s: _norm(str(s)))
    m = df[(cidade_norm == cn) & (df["UF"].astype(str).str.upper().str.strip() == un)]
    if m.empty:
        m = df[cidade_norm == cn]
    return m.iloc[0] if not m.empty else None


def _regiao_zone_index(regiao_code: str) -> int:
    """
    Infere o índice posicional (0=capital, 1=interior1, 2=interior2)
    a partir do código de REGIAO dos prazos.
    """
    r = _norm(regiao_code)

    # Indicadores de capital/polo (índice 0)
    if any(h in r for h in ["cap", "polo", "capital"]):
        return 0
    if re.search(r"(?:regiao|r)1$", r):
        return 0
    # Código 3 letras (hub codes de capital: VIX, BHZ, SSA, SAO…)
    if re.match(r"^[a-z]{3}$", r):
        return 0

    # Indicadores de interior 2 / mais distante (índice 2)
    if any(h in r for h in ["int2", "interior2", "interiori2", "i2", "int ii", "regiao2", "regiao 2"]):
        return 2
    if re.search(r"(?:regiao|r)2$", r) or r.endswith("2") or "i2" in r:
        return 2

    # Padrão: interior 1 (índice 1)
    if any(h in r for h in ["int", "interior", "regiao3", "regiao 3"]):
        return 1
    if re.search(r"(?:regiao|r)3$", r):
        return 2  # 3ª região → mais distante

    return 1  # default: interior 1


def _match_precos(precos_data: Dict, regiao: str, uf: str) -> Optional[pd.Series]:
    df = precos_data["df"]

    # Filtrar apenas linhas com UF válida (remove linhas de metadados)
    df = df[df["UF"].isin(_VALID_UF)]

    uf_u = uf.upper().strip()
    df_uf = df[df["UF"] == uf_u]
    if df_uf.empty:
        return None

    regiao_s = str(regiao).strip()

    # 1. Exact match
    mask = df_uf["REGIAO"] == regiao_s
    if df_uf[mask].shape[0]:
        return df_uf[mask].iloc[0]

    # 2. Case-insensitive + sem acento
    reg_norm = _norm(regiao_s)
    for _, row in df_uf.iterrows():
        if _norm(str(row["REGIAO"])) == reg_norm:
            return row

    # 3. Extrair sufixo após " - " (ex: "SP - POLO" → "POLO")
    if " - " in regiao_s:
        suffix = regiao_s.split(" - ")[-1].strip()
        suffix_norm = _norm(suffix)
        for _, row in df_uf.iterrows():
            rn = _norm(str(row["REGIAO"]))
            if rn == suffix_norm or suffix_norm in rn:
                return row

    # 4. Mobile: strip 2-char UF prefix de código alfanumérico (ES040 → 040)
    m = re.match(r"^[A-Z]{2}(\d{2,3})$", regiao_s)
    if m:
        code = m.group(1)
        for _, row in df_uf.iterrows():
            if str(row["REGIAO"]).strip() == code:
                return row

    # 5. Fuzzy por palavras-chave no REGIAO
    keywords = ["interior", "polo", "capital", "regiao"]
    reg_low = regiao_s.lower()
    for kw in keywords:
        if kw in reg_low:
            kw_matches = df_uf[[kw in str(r).lower() for r in df_uf["REGIAO"]]]
            if not kw_matches.empty:
                return kw_matches.iloc[0]

    # 6. Posicional: determinar índice pela semântica do código
    if regiao_s and regiao_s.lower() not in ("nan", ""):
        idx = _regiao_zone_index(regiao_s)
        idx = min(idx, len(df_uf) - 1)
        return df_uf.iloc[idx]

    # 7. Fallback: primeira linha do UF
    return df_uf.iloc[0]


def _taxas_condicionais(carrier_name, dest_cep, dest_uf, cnpj_destinatario, frete_base, valor_nf):
    """Consulta TDA/TRT/EMEX/TDE (falha silenciosa se o módulo/base não existir)."""
    try:
        from app import taxas_condicionais
        return taxas_condicionais.calcular(carrier_name, dest_cep, dest_uf,
                                           cnpj_destinatario or "", frete_base, valor_nf)
    except Exception:
        return {}


def _calc_charges(frete_peso: float, weight: float, declared_value: float,
                  meta: Dict, price_row: pd.Series,
                  dest_uf: str = "", origem_uf: str = "ES",
                  carrier_name: str = "", dest_cep: str = "",
                  cnpj_destinatario: str = "", regiao: str = "") -> Dict[str, float]:
    gris = max(declared_value * meta["gris_pct"], meta["gris_min"])
    # POS/TSO (Participação Obrigatória do Segurado) — % sobre a NF com mínimo.
    pos_pct = float(meta.get("pos_pct", 0) or 0)
    pos_min = float(meta.get("pos_min", 0) or 0)
    pos = max(declared_value * pos_pct, pos_min) if (pos_pct or pos_min) else 0.0
    ad_valorem = declared_value * float(price_row.get("advalorem_pct", 0) or 0)
    frete_val  = declared_value * float(price_row.get("frete_val_pct", 0) or 0)
    pedagio_r  = float(price_row.get("pedagio_col", 0) or 0) or meta["pedagio_ref"]
    pedagio    = math.ceil(weight / 100) * pedagio_r
    sec_cat    = float(price_row.get("sec_cat", 0) or 0)
    tas        = float(meta["tas"])
    # Despacho pode variar por rota (ex.: Taxa por CT-e maior no Norte/Nordeste):
    # usa o valor da linha de preço quando informado, senão o padrão do acordo (meta).
    despacho_row = price_row.get("despacho")
    despacho = float(despacho_row) if despacho_row not in (None, "") and pd.notna(despacho_row) else meta["despacho"]

    # Taxas condicionais por destino/destinatário (TDA/TRT/EMEX/TDE), calculadas
    # sobre o frete base (subtotal dos encargos antes de TEC/ICMS).
    frete_base = frete_peso + gris + pos + ad_valorem + frete_val + pedagio + tas + despacho + sec_cat
    cond = _taxas_condicionais(carrier_name, dest_cep, dest_uf, cnpj_destinatario, frete_base, declared_value)
    tda  = float(cond.get("tda", 0) or 0)
    emex = float(cond.get("emex", 0) or 0)
    tde  = float(cond.get("tde", 0) or 0)
    # POS condicional por CEP (ex.: TG só cobra no RJ) — soma ao POS de meta.
    pos_cond = float(cond.get("pos", 0) or 0)
    pos = pos + pos_cond
    # TRT: condicional por CEP (ex.: Fitlog) + TRT de generalidade da transportadora
    # (ex.: Jamef "TRT 1", mínimo fixo por CTRC). Somadas (não se sobrepõem).
    # trt_so_hub: a Jamef só cobra TRT 1 em cidades com tarifa própria (capitais e
    # polos regionais — código de região tipo JPA/MCZ/RAO), não nos códigos
    # genéricos de "Interior 1/2" do estado (sufixo I/I2, ex.: BAI2, CEI, PII2).
    trt_pct = float(meta.get("trt_pct", 0) or 0)
    trt_min = float(meta.get("trt_min", 0) or 0)
    trt_regiao_ok = True
    if meta.get("trt_so_hub") and re.search(r"i2?$", regiao or "", re.IGNORECASE):
        trt_regiao_ok = False
    trt_meta = max(frete_base * trt_pct, trt_min) if (trt_pct or trt_min) and trt_regiao_ok else 0.0
    trt = float(cond.get("trt", 0) or 0) + trt_meta

    # TEC (adicional de combustível): incide sobre o SUBTOTAL dos demais encargos
    # (padrão SSW/das transportadoras), não apenas sobre o frete peso.
    subtotal = frete_base + pos_cond + tda + trt + emex + tde
    tec = subtotal * float(meta.get("tec_pct", 0) or 0)

    comps = {
        "frete_peso": frete_peso, "gris": gris, "pos": pos, "ad_valorem": ad_valorem,
        "frete_val": frete_val, "tda": tda, "trt": trt, "emex": emex, "tde": tde,
        "tec": tec, "pedagio": pedagio, "tas": tas, "despacho": despacho, "sec_cat": sec_cat,
    }
    # ICMS pela alíquota da UF de destino.
    #  • Tarifa líquida (Fitlog/TG): soma o ICMS "por dentro" por cima do total.
    #  • Tarifa com ICMS embutido (Jamef): cada componente já é bruto — grosseia
    #    cada um por 1/(1-alíq) para bater item a item com o CTe; o ICMS vira só
    #    informativo (parcela embutida) e NÃO soma de novo (ver quote_for_carrier).
    # icms_aliq_fixa: algumas transportadoras (ex.: Fitlog) cobram uma alíquota
    # única no CT-e independente de a rota ser interna ou interestadual — quando
    # cadastrada, sobrepõe a regra genérica por UF.
    aliq_fixa = float(meta.get("icms_aliq_fixa", 0) or 0)
    aliq = aliq_fixa if aliq_fixa > 0 else icms_aliquota(dest_uf, origem_uf)
    if not (0 < aliq < 1):
        icms = 0.0
    elif meta.get("icms_embutido"):
        g = 1.0 / (1.0 - aliq)
        comps = {k: v * g for k, v in comps.items()}
        icms = sum(comps.values()) * aliq
    else:
        icms = (subtotal + tec) * (aliq / (1 - aliq))

    comps["icms"] = icms
    return {k: round(v, 2) for k, v in comps.items()}


def quote_for_carrier(
    carrier_name: str,
    precos_data: Dict,
    prazos_df: pd.DataFrame,
    dest_cep: str,
    total_billable: float,
    declared_value: float,
    city_uf: Optional[Tuple[str, str]] = None,
    today: Optional[date] = None,
    cnpj_destinatario: Optional[str] = None,
) -> Tuple[Optional[DeliveryMethod], str]:
    """
    Cotação para uma única transportadora. Reutilizado pelo auditor de CTe, pelo
    comparativo do cadastro e pelo romaneio (todos cotam por transportadora).
    Retorna (DeliveryMethod|None, motivo) — motivo explica a falha quando None.
    """
    today = today or date.today()
    meta = precos_data["meta"]

    prazo_row = _match_prazos_by_cep(prazos_df, dest_cep)
    if prazo_row is None and city_uf:
        prazo_row = _match_prazos_by_city(prazos_df, city_uf[0], city_uf[1])
    if prazo_row is None:
        return None, f"CEP/cidade de destino ({dest_cep}) não encontrado na tabela de prazos"

    regiao     = str(prazo_row.get("REGIAO", "")).strip()
    uf         = str(prazo_row.get("UF", "")).strip()
    prazo_dias = int(prazo_row.get("PRAZO_DIAS", 0))

    price_row = _match_precos(precos_data, regiao, uf)
    if price_row is None:
        return None, f"Sem tarifa cadastrada para UF={uf} REGIAO={regiao}"

    frete_peso = _get_frete_peso(price_row, total_billable)
    if frete_peso is None:
        return None, f"Peso {total_billable}kg fora das faixas cadastradas"

    origem_uf  = _uf_from_origin(precos_data.get("origin", ""))
    charges    = _calc_charges(frete_peso, total_billable, declared_value, meta, price_row,
                               dest_uf=uf, origem_uf=origem_uf, carrier_name=carrier_name,
                               dest_cep=dest_cep, cnpj_destinatario=cnpj_destinatario, regiao=regiao)
    total_cost = round(sum(charges.values()), 2)
    # ICMS embutido é parte da tarifa (não soma ao total, é só informativo).
    if meta.get("icms_embutido"):
        total_cost = round(total_cost - charges.get("icms", 0.0), 2)

    dm = DeliveryMethod(
        logistics_provider_name=carrier_name,
        delivery_method_name=precos_data.get("origin", ""),
        final_shipping_cost=total_cost,
        delivery_estimate_business_days=prazo_dias,
        delivery_estimate_date=_add_business_days(today, prazo_dias) if prazo_dias else None,
        carrier=CarrierInfo(name=carrier_name),
        charges=charges,
    )
    return dm, "OK"
