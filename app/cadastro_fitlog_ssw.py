"""
Importador da tabela de frete da Fitlog no formato SSW (workbook "TABELA VOLIX.xlsx").

A tabela da Fitlog vem no padrão SSW, muito diferente do modelo genérico. A aba
"TARIFA POR CIDADE" traz, por cidade: UF, praça (RAIO TABELA, ex. "SP - POLO" —
que é o mesmo código de região usado nos prazos da Fitlog no sistema), faixas de
peso 10/20/30/50/70/100 + excedente, ad valorem (Frete Valor), TAS, pedágio, GRIS
e faixa de CEP. Preços e encargos são constantes dentro de cada praça.

Estratégia (decidida com o usuário): atualizar SÓ o PREÇO (a tabela nova não traz
prazo em dias), mantendo os prazos atuais da Fitlog. Como 3 códigos de praça foram
renomeados, criamos apelidos para casar com as regiões dos prazos existentes.

Uso:  python -m app.cadastro_fitlog_ssw <arquivo.xlsx> [--dry-run]
"""
from __future__ import annotations

import sys
from collections import defaultdict
from typing import Dict, List

import pandas as pd

from app import cadastro

# Índices de coluna (0-based) na aba TARIFA POR CIDADE, dados a partir da linha 4.
C_CIDADE, C_UF, C_MACRO, C_PRACA = 1, 2, 3, 6
C_B10, C_B20, C_B30, C_B50, C_B70, C_B100, C_EXC = 7, 8, 9, 10, 11, 12, 13
C_FV, C_DESPACHO, C_TAS, C_PED, C_GRIS_PCT, C_GRIS_MIN = 14, 16, 19, 20, 21, 22
C_CEP_INI, C_CEP_FIM = 31, 32

# Praças renomeadas na tabela nova → código de região usado nos prazos atuais.
APELIDOS = {
    "MG - DIVINÓPOLIS": "DIV - POLO",
    "MG - EXTREMA POLO": "EXT - POLO",
    "MG - EXTREMA REGIÃO": "EXT - REGIÃO",
}


def _num(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return None


def parse_tarifa_por_cidade(path: str) -> List[Dict]:
    """Lê a aba TARIFA POR CIDADE e devolve uma linha de preço por praça."""
    raw = pd.read_excel(path, sheet_name="TARIFA POR CIDADE", header=None, dtype=str)
    df = raw.iloc[4:].reset_index(drop=True)

    por_praca: Dict[str, List] = defaultdict(list)
    for _, r in df.iterrows():
        praca = r[C_PRACA]
        if isinstance(praca, str) and praca.strip():
            por_praca[praca.strip()].append(r)

    linhas: List[Dict] = []
    for praca, rows in por_praca.items():
        r0 = rows[0]
        uf = str(r0[C_UF]).strip().upper()
        faixas = {}
        for bk, col in [("f10", C_B10), ("f20", C_B20), ("f30", C_B30),
                        ("f50", C_B50), ("f70", C_B70), ("f100", C_B100), ("ac200", C_EXC)]:
            v = _num(r0[col])
            if v and v > 0:
                faixas[bk] = v
        linha = {
            "uf": uf,
            "regiao": praca,
            "descricao": str(r0[C_MACRO]).strip() if isinstance(r0[C_MACRO], str) else "",
            "faixas": faixas,
            "advalorem_pct": _num(r0[C_FV]) or 0.0,
            "pedagio_col": _num(r0[C_PED]) or 0.0,
            "despacho": _num(r0[C_DESPACHO]),
            "sec_cat": 0.0,
            "frete_val_pct": 0.0,
            "_ncidades": len(rows),
        }
        linhas.append(linha)

    # Apelidos para as praças renomeadas (mesmos valores, código de região dos prazos)
    por_nome = {l["regiao"]: l for l in linhas}
    for origem, alias in APELIDOS.items():
        base = por_nome.get(origem)
        if base and alias not in por_nome:
            nova = dict(base)
            nova["regiao"] = alias
            nova.pop("_ncidades", None)
            linhas.append(nova)
    return linhas


# Meta (encargos padrão do acordo) — valores constantes conferidos na planilha.
META_FITLOG = {
    "cubagem_fator": 250,   # "Nossa cubagem segue 250 Kgs x M3" (Contrato e Perfil)
    "gris_pct": 0.002,      # GRIS 0,2%
    "gris_min": 5.7,        # GRIS mínimo R$5,70
    "tas": 5.45,            # TAS R$5,45
    "pedagio_ref": 7.4,     # Pedágio R$7,40/100kg (também por linha)
    "despacho": 10,         # Taxa por CT-e padrão (Sudeste/Sul); Norte/NE/Centro vem por linha (70)
    "tec_pct": 0,
    "origem": "São Paulo - SP",
}


def importar(path: str, nome: str = "Fitlog", dry_run: bool = False) -> Dict:
    cadastro.init_db()
    linhas = parse_tarifa_por_cidade(path)

    t = None
    for x in cadastro.list_transportadoras():
        if x["nome"].strip().lower() == nome.lower():
            t = x
            break
    if t is None:
        raise SystemExit(f"Transportadora '{nome}' não encontrada na base.")

    resumo = {
        "transportadora": t["nome"],
        "pracas": len(linhas),
        "despacho_10": sum(1 for l in linhas if l.get("despacho") == 10),
        "despacho_70": sum(1 for l in linhas if l.get("despacho") == 70),
        "amostra": [
            {k: l[k] for k in ("regiao", "uf", "faixas", "advalorem_pct", "despacho")}
            for l in linhas if l["regiao"] in ("SP - POLO", "AC - CAPITAL", "DIV - POLO")
        ],
    }
    if dry_run:
        resumo["modo"] = "DRY-RUN (nada gravado)"
        return resumo

    # Grava: substitui os preços da Fitlog e atualiza a meta; prazos ficam intactos.
    cadastro.update_transportadora(t["id"], META_FITLOG)
    cadastro.save_precos(t["id"], [{k: v for k, v in l.items() if not k.startswith("_")} for l in linhas])
    resumo["modo"] = "GRAVADO"
    resumo["prazos_mantidos"] = cadastro.list_prazos(t["id"], limit=1)["total"]
    return resumo


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    caminho = args[0] if args else r"C:\Users\andre\Downloads\TABELA VOLIX.xlsx"
    import json
    print(json.dumps(importar(caminho, dry_run=dry), ensure_ascii=False, indent=2))
