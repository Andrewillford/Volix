"""
Carrega TABELA_PRECOS e TABELA_PRAZOS no formato real das transportadoras.
"""
from __future__ import annotations

import re
import logging
from typing import Dict, Optional, Tuple, List

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# Faixas fixas ordenadas por limite (nome_interno, limite_kg)
WEIGHT_BANDS: List[Tuple[str, float]] = [
    ("f10",  10),
    ("f20",  20),
    ("f30",  30),
    ("f50",  50),
    ("f60",  60),
    ("f70",  70),
    ("f75",  75),
    ("f100", 100),
    ("f150", 150),
    ("f200", 200),
]


def _col_key(col: str) -> str:
    return re.sub(r"[^a-z0-9]", "", col.lower())


def _normalize_weight_col(col: str) -> Optional[str]:
    """
    Mapeia nome de coluna → chave interna.
    Faixas fixas: f10, f20, ..., f200
    Excedentes:   exc_150, exc_200 (Até Xkg excedente), ac200, ac1000, ac4000
    """
    k = _col_key(col)

    # Faixas fixas "F≤Xkg"
    for band_key, _ in WEIGHT_BANDS:
        limit = band_key[1:]  # "10","20",...
        if re.search(rf"f[≤e]?{limit}(kg)?$", k):
            return band_key

    # "Até 150 Kg excedente" → exc_150
    m = re.search(r"at[eé](\d+)kg?excedente", k)
    if m:
        return f"exc_{m.group(1)}"

    # "Ac 200 kg excedente" / "AC>200/kg" → ac200
    for thresh, key in [("4000","ac4000"), ("1000","ac1000"), ("200","ac200")]:
        if re.search(rf"ac[>_]?{thresh}", k) or re.search(rf"ac{thresh}kg?excedente", k):
            return key

    # "kg excedente" genérico → ac200
    if re.search(r"^kgexcedente$", k) or re.search(r"excedente$", k):
        return "ac200"

    return None


def _parse_header_info(text: str) -> Dict[str, float]:
    info = {"gris_pct": 0.0, "gris_min": 0.0, "pedagio_ref": 0.0,
            "tas": 0.0, "despacho": 0.0, "tec_pct": 0.0, "cubagem_fator": 300.0}
    if not isinstance(text, str):
        return info
    patterns = {
        "gris_pct":      r"GRIS\s+([\d.,]+)%",
        "gris_min":      r"GRIS[^|]*m[íi]n\s*R\$\s*([\d.,]+)",
        "pedagio_ref":   r"Ped[aá]gio\s+ref\s+R\$\s*([\d.,]+)",
        "tas":           r"TAS\s+R\$\s*([\d.,]+)",
        "despacho":      r"Despacho\s+R\$\s*([\d.,]+)",
        "tec_pct":       r"TEC\s+([\d.,]+)%",
        "cubagem_fator": r"Cubagem\s+([\d.,]+)\s*kg",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                info[key] = float(m.group(1).replace(",", "."))
            except ValueError:
                pass
    info["gris_pct"] /= 100
    info["tec_pct"] /= 100
    return info


def _parse_prazo(val) -> int:
    nums = re.findall(r"\d+", str(val))
    return max(int(n) for n in nums) if nums else 0


# ─── PRECOS ─────────────────────────────────────────────────────────────────

def _load_precos_sheet(df_raw: pd.DataFrame, carrier_name: str) -> Optional[Dict]:
    header_row0 = str(df_raw.iloc[0, 0]) if not df_raw.empty else ""
    origin_match = re.search(r"origem:\s*(.+)", header_row0, re.IGNORECASE)
    origin = origin_match.group(1).strip() if origin_match else ""

    header_row1 = str(df_raw.iloc[1, 0]) if len(df_raw) > 1 else ""
    meta = _parse_header_info(header_row1)

    header_idx = None
    for i in range(min(6, len(df_raw))):
        if str(df_raw.iloc[i, 0]).strip().upper() == "UF":
            header_idx = i
            break
    if header_idx is None:
        logger.warning(f"[{carrier_name}/precos] Cabeçalho UF não encontrado.")
        return None

    df = df_raw.iloc[header_idx:].reset_index(drop=True)
    df.columns = [str(c).strip() for c in df.iloc[0]]
    df = df.iloc[1:].reset_index(drop=True)
    df = df.dropna(subset=[df.columns[0]])

    # Mapear colunas de faixa de peso — sem duplicatas (primeiro vence)
    band_cols: Dict[str, str] = {}
    seen_keys: set = set()
    rename: Dict[str, str] = {}
    for col in df.columns:
        bk = _normalize_weight_col(col)
        if bk and bk not in seen_keys:
            band_cols[col] = bk
            rename[col] = bk
            seen_keys.add(bk)
        elif bk and bk in seen_keys:
            # Coluna duplicada — ignorar
            rename[col] = f"_dup_{col}"

    # Colunas de encargos por rota
    extra_rename: Dict[str, str] = {}
    for col in df.columns:
        if col in rename:
            continue
        k = _col_key(col)
        if "advalorem" in k:
            extra_rename[col] = "advalorem_pct"
        elif "freteval" in k or "frete_val" in k:
            extra_rename[col] = "frete_val_pct"
        elif ("seccat" in k or "sec_cat" in k) and col not in rename:
            extra_rename[col] = "sec_cat"
        elif "pedagio" in k and "100" in k:
            extra_rename[col] = "pedagio_col"

    df = df.rename(columns={**rename, **extra_rename})
    # Remover colunas duplicadas geradas
    df = df[[c for c in df.columns if not c.startswith("_dup_")]]

    for col in ("UF", "REGIAO"):
        if col not in df.columns:
            df[col] = ""
    df["UF"] = df["UF"].astype(str).str.strip().str.upper()
    df["REGIAO"] = df["REGIAO"].astype(str).str.strip()

    all_band_keys = list(seen_keys)
    for bk in all_band_keys:
        if bk in df.columns:
            df[bk] = pd.to_numeric(df[bk], errors="coerce")

    for col_name in ("advalorem_pct", "frete_val_pct", "sec_cat", "pedagio_col"):
        if col_name not in df.columns:
            df[col_name] = 0.0
        else:
            df[col_name] = pd.to_numeric(df[col_name], errors="coerce").fillna(0.0)

    return {
        "carrier": carrier_name,
        "origin": origin,
        "meta": meta,
        "band_keys": sorted(seen_keys),
        "df": df,
    }


def load_precos(path: str) -> Dict[str, Dict]:
    import os
    if not os.path.exists(path):
        logger.info(f"Planilha de preços não encontrada ({path}); usando só a base de acordo.")
        return {}
    xls = pd.ExcelFile(path)
    result = {}
    for sheet in xls.sheet_names:
        if sheet.upper().startswith("INSTRUC"):
            continue
        try:
            raw = pd.read_excel(xls, sheet_name=sheet, header=None, dtype=str)
            data = _load_precos_sheet(raw, sheet)
            if data:
                result[sheet] = data
                logger.info(f"  [{sheet}] origem={data['origin']} bandas={data['band_keys']}")
        except Exception as e:
            logger.error(f"Erro aba {sheet} (precos): {e}", exc_info=True)
    return result


# ─── PRAZOS ─────────────────────────────────────────────────────────────────

def _load_prazos_sheet(df_raw: pd.DataFrame, carrier_name: str) -> Optional[pd.DataFrame]:
    header_idx = None
    for i in range(min(5, len(df_raw))):
        row_str = " ".join(str(v) for v in df_raw.iloc[i].values)
        if "UF_DESTINO" in row_str.upper() or "UFDESTINO" in row_str.upper().replace("_",""):
            header_idx = i
            break
    if header_idx is None:
        return None

    df = df_raw.iloc[header_idx:].reset_index(drop=True)
    df.columns = [re.sub(r"\*", "", str(c)).strip().upper() for c in df.iloc[0]]
    df = df.iloc[1:].reset_index(drop=True)

    col_map = {}
    for col in df.columns:
        k = _col_key(col)
        if "ufdestino" in k:
            col_map[col] = "UF"
        elif "cidadedestino" in k:
            col_map[col] = "CIDADE"
        elif "prazodia" in k or "prazo" in k:
            col_map[col] = "PRAZO_DIAS"
        elif k == "regiao":
            col_map[col] = "REGIAO"
        elif "cepinicial" in k:
            col_map[col] = "CEP_INICIAL"
        elif "cepfinal" in k:
            col_map[col] = "CEP_FINAL"
    df = df.rename(columns=col_map)

    for col in ("UF", "CIDADE", "PRAZO_DIAS", "REGIAO", "CEP_INICIAL", "CEP_FINAL"):
        if col not in df.columns:
            df[col] = np.nan

    df = df.dropna(subset=["UF", "PRAZO_DIAS"])
    df["UF"] = df["UF"].astype(str).str.strip().str.upper()
    df["CIDADE"] = df["CIDADE"].astype(str).str.strip().str.upper()
    df["REGIAO"] = df["REGIAO"].astype(str).str.strip()
    df["PRAZO_DIAS"] = df["PRAZO_DIAS"].apply(_parse_prazo)

    # CEP como string zerofill 8 — manter como string para comparação correta
    def _norm_cep(v):
        try:
            return str(int(float(str(v).replace(",","")))).zfill(8)
        except Exception:
            return None

    df["CEP_INICIAL"] = df["CEP_INICIAL"].apply(_norm_cep)
    df["CEP_FINAL"]   = df["CEP_FINAL"].apply(_norm_cep)
    df["has_cep"] = df["CEP_INICIAL"].notna() & df["CEP_FINAL"].notna()

    return df.reset_index(drop=True)


def load_prazos(path: str) -> Dict[str, pd.DataFrame]:
    import os
    if not os.path.exists(path):
        logger.info(f"Planilha de prazos não encontrada ({path}); usando só a base de acordo.")
        return {}
    xls = pd.ExcelFile(path)
    result = {}
    for sheet in xls.sheet_names:
        if sheet.upper().startswith("INSTRUC"):
            continue
        try:
            raw = pd.read_excel(xls, sheet_name=sheet, header=None, dtype=str)
            df = _load_prazos_sheet(raw, sheet)
            if df is not None and not df.empty:
                result[sheet] = df
                logger.info(f"  [{sheet}] {len(df)} cidades | {df['has_cep'].sum()} com CEP")
        except Exception as e:
            logger.error(f"Erro aba {sheet} (prazos): {e}", exc_info=True)
    return result
