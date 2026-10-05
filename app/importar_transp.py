"""
Importadores das tabelas reais de Fitlog e TG para a base de acordo.

Cada transportadora vem em formato próprio (Fitlog no padrão SSW; TG numa planilha
de tarifa por praça), com um arquivo de PREÇO e outro de PRAZO:

  Fitlog: preço = "TABELA VOLIX FITLOG.xlsx" (aba TARIFA POR CIDADE, reusa
          app.cadastro_fitlog_ssw.parse_tarifa_por_cidade); prazo = "RELAÇÃO DE
          PRAÇAS E PRAZOS ES fitlog.xlsx" (aba FIT, origem VIX = Viana-ES).
  TG:     preço = "TG - VOLIX ...xlsx" (aba única, praças com bandas 30/50/70/100 +
          excedente + ad valorem); prazo = "Prazos com TDA TG.xlsx" (CIDADES ATENDIDAS).

Estratégia: a linha de PREÇO é gerada por (UF, praça) a partir das combinações que
aparecem no PRAZO — assim o casamento UF+REGIÃO do motor sempre acha a tarifa, mesmo
quando uma praça atende mais de um estado (ex.: TG VTAI = ES+MG, BSBP = DF+GO).

Uso:  python -m app.importar_transp            # importa Fitlog e TG (default paths)
      python -m app.importar_transp --zerar    # zera a base antes de importar
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

import pandas as pd

from app import cadastro
from app.cadastro_fitlog_ssw import META_FITLOG, parse_tarifa_por_cidade

BASE_DIR = os.getenv("TRANSP_DIR", r"C:\Users\andre\Downloads\transp")

FITLOG_PRECO = os.path.join(BASE_DIR, "TABELA VOLIX FITLOG.xlsx")
FITLOG_PRAZO = os.path.join(BASE_DIR, "RELAÇÃO DE PRAÇAS E PRAZOS ES fitlog.xlsx")
TG_PRECO = os.path.join(BASE_DIR, "TG - VOLIX TECNOLOGIA E SOLUCOES LTDA.xlsx")
TG_PRAZO = os.path.join(BASE_DIR, "Prazos com TDA TG.xlsx")

# TG: generalidades conferidas nas notas do arquivo (o que não vem numérico fica 0
# e deve ser revisado no painel). Bandas + ad valorem (por linha) + TEC 4,5%.
# Generalidades da PROPOSTA TG (2026): pedágio 7,5 · GRIS 0,3% mín 5,04 · TAS 3,48
# · despacho 15 · cubagem 300 · TEC 4,5%. O 0,3% do campo ADVALOREM da planilha é,
# no CTe, "Frete Valor Declarado" → gravado como frete_val_pct (não advalorem).
META_TG = {
    "origem": "Grande Vitória-ES",
    "cubagem_fator": 300,
    "gris_pct": 0.003,
    "gris_min": 5.04,
    "tas": 3.48,
    "despacho": 15.0,
    "pedagio_ref": 7.5,
    "tec_pct": 0.045,
}


def _s(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def _num(v) -> Optional[float]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _cep(v) -> Optional[str]:
    s = "".join(c for c in _s(v) if c.isdigit())
    return s.zfill(8) if s else None


def _get_or_create(nome: str, meta: Dict) -> Dict:
    for x in cadastro.list_transportadoras():
        if x["nome"].strip().lower() == nome.strip().lower():
            cadastro.update_transportadora(x["id"], meta)
            return cadastro.get_transportadora(x["id"])
    return cadastro.create_transportadora({"nome": nome, **meta})


def _precos_por_uf_praca(prazos: List[Dict], praca_map: Dict[str, Dict]) -> List[Dict]:
    """Uma linha de preço por (UF, praça) presente no prazo, com a tarifa da praça.
    Garante que o casamento UF+REGIÃO do motor sempre ache a tarifa."""
    rows, seen = [], set()
    faltando = set()
    for pz in prazos:
        chave = (pz["uf"], pz["regiao"])
        if chave in seen:
            continue
        base = praca_map.get(pz["regiao"])
        if base is None:
            faltando.add(pz["regiao"])
            continue
        seen.add(chave)
        rows.append({"uf": pz["uf"], "regiao": pz["regiao"],
                     "descricao": base.get("descricao", ""), "faixas": base["faixas"],
                     "advalorem_pct": base.get("advalorem_pct", 0.0),
                     "frete_val_pct": base.get("frete_val_pct", 0.0),
                     "sec_cat": base.get("sec_cat", 0.0),
                     "pedagio_col": base.get("pedagio_col", 0.0),
                     "despacho": base.get("despacho")})
    if faltando:
        print(f"  [aviso] praças no prazo sem preço: {sorted(faltando)}")
    return rows


# ─── FITLOG ──────────────────────────────────────────────────────────────────

def parse_fit_prazos(path: str, origem: str = "VIX") -> List[Dict]:
    """Prazos da aba FIT filtrando a origem (VIX = Viana-ES, que atende a Volix)."""
    raw = pd.read_excel(path, sheet_name="FIT", header=None, dtype=str)
    out = []
    for _, r in raw.iloc[5:].iterrows():
        if _s(r[3]) != origem or not _s(r[7]):
            continue
        out.append({"uf": _s(r[7]).upper(), "cidade": _s(r[8]).upper(), "regiao": _s(r[17]),
                    "cep_inicial": _cep(r[18]), "cep_final": _cep(r[19]),
                    "prazo_dias": int(_num(r[11]) or 0), "tda": _s(r[14])})
    return out


def importar_fitlog(preco_path: str = FITLOG_PRECO, prazo_path: str = FITLOG_PRAZO,
                    nome: str = "Fitlog", origem: str = "VIX") -> Dict:
    linhas_preco = parse_tarifa_por_cidade(preco_path)  # já inclui apelidos DIV/EXT
    praca_map = {l["regiao"]: l for l in linhas_preco}
    prazos = parse_fit_prazos(prazo_path, origem)
    precos = _precos_por_uf_praca(prazos, praca_map)

    meta = dict(META_FITLOG)
    meta["origem"] = "Viana-ES"  # origem real de coleta p/ a Volix
    t = _get_or_create(nome, meta)
    cadastro.save_precos(t["id"], precos)
    cadastro.save_prazos(t["id"], prazos)
    return {"transportadora": t["nome"], "pracas_preco": len(precos),
            "prazos": len(prazos), "cidades": len(prazos)}


# ─── TG ──────────────────────────────────────────────────────────────────────

def parse_tg_precos_map(path: str) -> Dict[str, Dict]:
    """Mapa praça → tarifa. Expande praças compostas ('UDII/UBAP/PTMP') e cria
    apelidos MT1I..MT5I → MT1..MT5 (o prazo usa o sufixo 'I')."""
    raw = pd.read_excel(path, sheet_name=0, header=None, dtype=str)
    m: Dict[str, Dict] = {}
    for _, r in raw.iloc[13:].iterrows():
        praca = _s(r[5])
        if not praca or praca.lower() == "nan":
            continue
        faixas = {}
        for bk, col in [("f30", 6), ("f50", 7), ("f70", 8), ("f100", 9), ("ac200", 10)]:
            v = _num(r[col])
            if v and v > 0:
                faixas[bk] = round(v, 4)
        # O 0,3% da coluna ADVALOREM é o "Frete Valor Declarado" no CTe do TG →
        # grava como frete_val_pct (não advalorem), senão a auditoria duplica.
        tarifa = {"faixas": faixas, "advalorem_pct": 0.0, "frete_val_pct": _num(r[11]) or 0.0,
                  "descricao": _s(r[3]), "pedagio_col": 0.0,
                  "sec_cat": 0.0, "despacho": None}
        for sub in praca.split("/"):
            m[sub.strip()] = tarifa
    for k in ("MT1", "MT3", "MT4", "MT5"):
        if k in m:
            m[k + "I"] = m[k]
    return m


def parse_tg_prazos(path: str) -> List[Dict]:
    xls = pd.ExcelFile(path)
    aba = next((s for s in xls.sheet_names if "CIDADES ATENDIDAS" in s.upper()), xls.sheet_names[0])
    raw = pd.read_excel(path, sheet_name=aba, header=None, dtype=str)
    out = []
    for _, r in raw.iloc[6:].iterrows():
        if not _s(r[6]):
            continue
        out.append({"uf": _s(r[5]).upper(), "cidade": _s(r[6]).upper(), "regiao": _s(r[9]),
                    "cep_inicial": _cep(r[10]), "cep_final": _cep(r[11]),
                    "prazo_dias": int(_num(r[7]) or 0), "tda": _s(r[8])})
    return out


def importar_tg(preco_path: str = TG_PRECO, prazo_path: str = TG_PRAZO, nome: str = "TG") -> Dict:
    praca_map = parse_tg_precos_map(preco_path)
    prazos = parse_tg_prazos(prazo_path)
    precos = _precos_por_uf_praca(prazos, praca_map)
    t = _get_or_create(nome, META_TG)
    cadastro.save_precos(t["id"], precos)
    cadastro.save_prazos(t["id"], prazos)
    return {"transportadora": t["nome"], "linhas_preco": len(precos),
            "prazos": len(prazos), "cidades": len(prazos)}


# ─── JAMEF ───────────────────────────────────────────────────────────────────

# Generalidades da Jamef (rodapé da "Tabela de Fretes FIPE" / Volix Tecnologia ajuste):
# GRIS 0,16% mín 3,99 · pedágio 9,68/100kg · TAS 7,71 · taxa CTRC (despacho) 7,50 ·
# TEC 3,75% sobre o frete · cubagem 300 · sem POS · sem TRT. Ad valorem por linha é
# "Frete Valor" no CTe → frete_val_pct.
META_JAMEF = {
    "origem": "Vitória-ES",
    "cubagem_fator": 300,
    "gris_pct": 0.0016, "gris_min": 3.99,
    "tas": 7.71, "despacho": 7.50, "pedagio_ref": 9.68,
    "tec_pct": 0.0375, "pos_pct": 0.0, "pos_min": 0.0,
}


def parse_jamef_precos_map(path: str) -> Dict[str, Dict]:
    """Mapa SIGLA → tarifa (faixas 10/20/30/50/75/100 + excedente + frete_val)."""
    raw = pd.read_excel(path, sheet_name=0, header=None, dtype=str)
    m: Dict[str, Dict] = {}
    for _, r in raw.iloc[9:].iterrows():
        sigla = _s(r[2])
        if not sigla or sigla.lower() == "nan":
            continue
        faixas = {}
        for bk, col in [("f10", 4), ("f20", 5), ("f30", 6), ("f50", 7),
                        ("f75", 8), ("f100", 9), ("ac200", 10)]:
            v = _num(r[col])
            if v and v > 0:
                faixas[bk] = round(v, 4)
        m[sigla] = {"faixas": faixas, "advalorem_pct": 0.0, "frete_val_pct": _num(r[11]) or 0.0,
                    "descricao": _s(r[3]), "pedagio_col": 0.0, "sec_cat": 0.0, "despacho": None}
    # Apelido: no prazo o interior-1 vem sem o "1" (ex.: 'ALI' ↔ preço 'ALI1').
    for sigla in list(m):
        if sigla.endswith("1") and sigla[:-1] and sigla[:-1] not in m:
            m[sigla[:-1]] = m[sigla]
    return m


def parse_jamef_prazos(path: str) -> List[Dict]:
    """Prazos da Jamef: cidade/UF destino, praça = SIGLA TARIFARIO, CEP, prazo."""
    raw = pd.read_excel(path, sheet_name=0, header=None, dtype=str)
    # cabeçalho tem 'SIGLA TARIFARIO' e 'MIN_CEP'
    hdr = next((i for i in range(min(15, len(raw)))
                if any("SIGLA TARIFARIO" in str(v).upper() for v in raw.iloc[i].values if v)), 8)
    out = []
    for _, r in raw.iloc[hdr + 1:].iterrows():
        cidade = _s(r[3])
        if not cidade:
            continue
        out.append({"uf": _s(r[4]).upper(), "cidade": cidade.upper(), "regiao": _s(r[6]),
                    "cep_inicial": _cep(r[8]), "cep_final": _cep(r[9]),
                    "prazo_dias": int(_num(r[10]) or 0), "tda": ""})
    return out


def importar_jamef(preco_path: str, prazo_path: str, nome: str = "Jamef Encomendas Urgentes") -> Dict:
    praca_map = parse_jamef_precos_map(preco_path)
    prazos = parse_jamef_prazos(prazo_path)
    precos = _precos_por_uf_praca(prazos, praca_map)
    t = _get_or_create(nome, META_JAMEF)
    cadastro.save_precos(t["id"], precos)
    cadastro.save_prazos(t["id"], prazos)
    return {"transportadora": t["nome"], "linhas_preco": len(precos),
            "prazos": len(prazos), "cidades": len(prazos)}


def zerar_base() -> int:
    n = 0
    for t in cadastro.list_transportadoras():
        cadastro.delete_transportadora(t["id"])
        n += 1
    return n


def main(zerar: bool = False) -> Dict:
    cadastro.init_db()
    resultado: Dict = {}
    if zerar:
        resultado["removidas"] = zerar_base()
    resultado["fitlog"] = importar_fitlog()
    resultado["tg"] = importar_tg()
    resultado["base"] = sorted(t["nome"] for t in cadastro.list_transportadoras())
    return resultado


if __name__ == "__main__":
    import json
    print(json.dumps(main(zerar="--zerar" in sys.argv), ensure_ascii=False, indent=2))
