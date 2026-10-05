"""
Modelo padrão de planilha para importar/exportar tabelas de frete no cadastro.

Um único workbook Excel com as abas:
  • Instruções      — como preencher
  • Transportadora  — dados do acordo (nome, CNPJs, metas, encargos padrão)
  • Precos          — tarifa por UF/Região (faixas de peso + encargos por rota)
  • Prazos          — prazo de entrega por cidade/UF (com faixa de CEP opcional)

`gerar_modelo()` produz o arquivo (vazio com exemplo, ou preenchido com os dados
de uma transportadora). `ler_planilha()` faz o caminho inverso, de forma tolerante
a variações de cabeçalho (aceita o modelo e também o formato das planilhas antigas).

gris_pct e tec_pct trafegam na planilha em PERCENTUAL (ex.: 0,30 = 0,30%); o banco
guarda como fração — a conversão ×100 / ÷100 é feita aqui.
"""
from __future__ import annotations

import io
import re
from typing import Dict, List, Optional

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.cadastro import BAND_KEYS

# Ordem e rótulo das faixas de peso nas colunas da aba Precos.
BANDA_COLS: List[tuple] = [
    ("f10", "ATE_10KG"), ("f20", "ATE_20KG"), ("f30", "ATE_30KG"), ("f50", "ATE_50KG"),
    ("f60", "ATE_60KG"), ("f70", "ATE_70KG"), ("f75", "ATE_75KG"), ("f100", "ATE_100KG"), ("f150", "ATE_150KG"),
    ("f200", "ATE_200KG"), ("exc_150", "EXC_150_RS_KG"), ("exc_200", "EXC_200_RS_KG"),
    ("ac200", "ACIMA_200_RS_KG"), ("ac1000", "ACIMA_1000_RS_KG"), ("ac4000", "ACIMA_4000_RS_KG"),
]
PRECO_EXTRA_COLS = [("advalorem_pct", "ADVALOREM"), ("frete_val_pct", "FRETE_VAL"),
                    ("sec_cat", "SEC_CAT"), ("pedagio_col", "PEDAGIO_100KG"),
                    ("despacho", "DESPACHO")]

# Rótulos amigáveis dos campos da aba Transportadora. gris/tec marcados como %.
TRANSP_CAMPOS = [
    ("nome", "Nome (identificador)", ""),
    ("apelido", "Apelido", ""),
    ("origem", "Origem", ""),
    ("cnpjs", "CNPJs (separados por vírgula)", ""),
    ("cor", "Cor (hex)", "#8bc34a"),
    ("ativo", "Ativa? (SIM/NAO)", "SIM"),
    ("meta_otd", "Meta OTD (%)", 95),
    ("atencao_otd", "Atenção OTD (%)", 85),
    ("tol_div_abs", "Tolerância divergência (R$)", 1),
    ("tol_div_pct", "Tolerância divergência (%)", 2),
    ("gris_pct", "GRIS (%)", ""),
    ("gris_min", "GRIS mínimo (R$)", ""),
    ("pedagio_ref", "Pedágio ref (R$/100kg)", ""),
    ("tas", "TAS (R$)", ""),
    ("despacho", "Despacho (R$)", ""),
    ("tec_pct", "TEC (%)", ""),
    ("cubagem_fator", "Cubagem (kg/m³)", 300),
]
_CAMPO_POR_LABEL = {re.sub(r"[^a-z0-9]", "", lbl.lower()): key for key, lbl, _ in TRANSP_CAMPOS}
_PCT_FIELDS = {"gris_pct", "tec_pct"}  # gravados como fração no banco

_HEAD_FILL = PatternFill("solid", fgColor="1F2A44")
_HEAD_FONT = Font(bold=True, color="FFFFFF")
_TITLE_FONT = Font(bold=True, size=13, color="2E7D32")
_THIN = Side(style="thin", color="C9D2E0")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _col_para_banda(header: str) -> Optional[str]:
    """Cabeçalho de coluna de faixa → chave interna (f10, exc_150, ac200...).
    Aceita o modelo (ATE_10KG), as chaves do banco (f10) e o formato antigo (F≤10kg)."""
    k = _norm(header)
    if k in BAND_KEYS:
        return k
    m = re.match(r"^ate(\d+)kg?$", k)
    if m and f"f{m.group(1)}" in BAND_KEYS:
        return f"f{m.group(1)}"
    m = re.search(r"exc(\d+)", k)
    if m and f"exc_{m.group(1)}" in BAND_KEYS:
        return f"exc_{m.group(1)}"
    m = re.search(r"ac(?:ima)?(\d+)", k)
    if m and f"ac{m.group(1)}" in BAND_KEYS:
        return f"ac{m.group(1)}"
    # fallback: normalizador tolerante das planilhas antigas ("F≤10kg", "kg excedente")
    try:
        from app.loaders import _normalize_weight_col
        return _normalize_weight_col(header)
    except Exception:
        return None


# ─── Geração do modelo ───────────────────────────────────────────────────────

def _estilo_cabecalho(ws, ncols: int, row: int = 1) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = _HEAD_FILL
        cell.font = _HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _BORDER
    ws.row_dimensions[row].height = 26


def _aba_instrucoes(wb) -> None:
    ws = wb.create_sheet("Instruções")
    linhas = [
        ("MODELO DE TABELA DE FRETE — VOLIX", ""),
        ("", ""),
        ("Preencha as abas e importe no painel Cadastro de Tabelas → “Importar planilha”.", ""),
        ("A transportadora é identificada pelo NOME (aba Transportadora). Se já existir, é atualizada; senão, criada.", ""),
        ("Ao importar, as linhas de Preço e Prazo SUBSTITUEM as atuais dessa transportadora.", ""),
        ("", ""),
        ("Aba Transportadora", "dados do acordo (uma linha por campo)."),
        ("  • CNPJs", "os CNPJs emitentes do CTe, separados por vírgula."),
        ("  • GRIS (%) e TEC (%)", "em percentual — ex.: 0,30 significa 0,30%."),
        ("  • Cubagem", "fator kg/m³ para peso cubado (padrão 300)."),
        ("", ""),
        ("Aba Precos", "uma linha por rota (UF + Região)."),
        ("  • ATE_XXKG", "valor do frete em R$ para peso até XX kg."),
        ("  • EXC_150 / ACIMA_200 ...", "R$ por kg no excedente (deixe em branco se não usar)."),
        ("  • ADVALOREM / FRETE_VAL", "fração da NF conforme o acordo (ex.: 0,0037)."),
        ("  • SEC_CAT / PEDAGIO_100KG", "R$ por rota / R$ por 100 kg."),
        ("  • Deixe em branco toda faixa/encargo que não existir.", ""),
        ("", ""),
        ("Aba Prazos", "uma linha por cidade/UF."),
        ("  • CEP_INICIAL / CEP_FINAL", "opcional (8 dígitos). Se preenchido, tem prioridade no casamento."),
        ("  • Sem CEP", "casa por CIDADE + UF (use o nome exato da cidade)."),
        ("  • PRAZO_DIAS", "prazo em dias úteis."),
    ]
    for r, (a, b) in enumerate(linhas, start=1):
        ws.cell(row=r, column=1, value=a)
        ws.cell(row=r, column=2, value=b)
    ws["A1"].font = _TITLE_FONT
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 62


def _aba_transportadora(wb, transp: Optional[Dict]) -> None:
    ws = wb.create_sheet("Transportadora")
    ws.cell(row=1, column=1, value="CAMPO")
    ws.cell(row=1, column=2, value="VALOR")
    _estilo_cabecalho(ws, 2)
    for i, (key, label, default) in enumerate(TRANSP_CAMPOS, start=2):
        ws.cell(row=i, column=1, value=label)
        if transp is not None:
            if key == "cnpjs":
                val = ", ".join(transp.get("cnpjs", []))
            elif key == "ativo":
                val = "SIM" if transp.get("ativo", True) else "NAO"
            elif key in _PCT_FIELDS:
                v = transp.get(key)
                val = round(v * 100, 6) if v not in (None, "") else ""
            else:
                val = transp.get(key, "")
            if val is None:
                val = ""
        else:
            val = default
        ws.cell(row=i, column=2, value=val).border = _BORDER
        ws.cell(row=i, column=1).border = _BORDER
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 40


def _aba_precos(wb, linhas: Optional[List[Dict]]) -> None:
    ws = wb.create_sheet("Precos")
    headers = ["UF", "REGIAO", "DESCRICAO"] + [h for _, h in BANDA_COLS] + [h for _, h in PRECO_EXTRA_COLS]
    ws.append(headers)
    _estilo_cabecalho(ws, len(headers))
    exemplo = [{"uf": "SP", "regiao": "010", "descricao": "Capital",
                "faixas": {"f30": 57, "f50": 68, "f100": 83}, "pedagio_col": 8}]
    for l in (linhas if linhas else exemplo):
        row = [l.get("uf", ""), l.get("regiao", ""), l.get("descricao", "")]
        faixas = l.get("faixas", {}) or {}
        row += [faixas.get(bk, "") for bk, _ in BANDA_COLS]
        row += [l.get(k, "") if l.get(k) not in (None, 0) else "" for k, _ in PRECO_EXTRA_COLS]
        ws.append(row)
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 30
    for c in range(4, len(headers) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 13
    ws.freeze_panes = "D2"


def _aba_prazos(wb, linhas: Optional[List[Dict]]) -> None:
    ws = wb.create_sheet("Prazos")
    headers = ["UF", "CIDADE", "REGIAO", "CEP_INICIAL", "CEP_FINAL", "PRAZO_DIAS"]
    ws.append(headers)
    _estilo_cabecalho(ws, len(headers))
    exemplo = [{"uf": "SP", "cidade": "SAO PAULO", "regiao": "010",
                "cep_inicial": "01000000", "cep_final": "01999999", "prazo_dias": 3}]
    for l in (linhas if linhas else exemplo):
        ws.append([l.get("uf", ""), l.get("cidade", ""), l.get("regiao", ""),
                   l.get("cep_inicial") or "", l.get("cep_final") or "", l.get("prazo_dias", "")])
    for col, w in zip("ABCDEF", (6, 30, 12, 14, 14, 12), strict=False):
        ws.column_dimensions[col].width = w
    # CEP como texto para preservar zeros à esquerda
    for r in range(2, ws.max_row + 1):
        for c in (4, 5):
            ws.cell(row=r, column=c).number_format = "@"
    ws.freeze_panes = "A2"


def gerar_modelo(transp: Optional[Dict] = None,
                 precos: Optional[List[Dict]] = None,
                 prazos: Optional[List[Dict]] = None) -> bytes:
    """Gera o workbook-modelo. Sem argumentos → modelo em branco com exemplos.
    Com dados → exportação preenchida de uma transportadora."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # remove a aba padrão
    _aba_instrucoes(wb)
    _aba_transportadora(wb, transp)
    _aba_precos(wb, precos)
    _aba_prazos(wb, prazos)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ─── Leitura do modelo ───────────────────────────────────────────────────────

def _acha_aba(wb, *chaves: str):
    """Encontra a aba cujo nome contém uma das chaves (case/acento-insensível)."""
    for ws in wb.worksheets:
        n = _norm(ws.title)
        if any(ch in n for ch in chaves):
            return ws
    return None


def _linha_cabecalho(ws, obrig: str):
    """Localiza a linha de cabeçalho (a que contém a coluna obrigatória) e devolve
    (índice_linha, {coluna_idx: header})."""
    for r in range(1, min(ws.max_row, 8) + 1):
        headers = {c: ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)}
        if any(_norm(v) == _norm(obrig) or _norm(obrig) in _norm(v) for v in headers.values() if v):
            return r, headers
    return None, {}


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(".", "").replace(",", ".")) if isinstance(v, str) and "," in v else float(v)
    except (ValueError, TypeError):
        return None


def ler_planilha(data: bytes) -> Dict:
    """Lê o workbook e devolve {transportadora, precos, prazos}. Tolerante a
    variações de nome de aba e de cabeçalho."""
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    resultado: Dict = {"transportadora": {}, "precos": [], "prazos": []}

    # ── Transportadora (chave/valor) ──
    ws = _acha_aba(wb, "transportadora", "acordo", "dados")
    transp: Dict = {}
    if ws is not None:
        for r in range(1, ws.max_row + 1):
            label = ws.cell(row=r, column=1).value
            valor = ws.cell(row=r, column=2).value
            key = _CAMPO_POR_LABEL.get(_norm(label))
            if not key or valor in (None, ""):
                continue
            if key == "cnpjs":
                transp["cnpjs"] = [re.sub(r"\D", "", c) for c in re.split(r"[,;\n]+", str(valor)) if re.sub(r"\D", "", c)]
            elif key == "ativo":
                transp["ativo"] = _norm(valor) in ("sim", "s", "1", "true", "ativa", "ativo")
            elif key in _PCT_FIELDS:
                f = _to_float(valor)
                transp[key] = f / 100 if f is not None else None
            elif key in ("nome", "apelido", "origem", "cor"):
                transp[key] = str(valor).strip()
            else:
                transp[key] = _to_float(valor)
    resultado["transportadora"] = transp

    # ── Precos ──
    ws = _acha_aba(wb, "prec", "tarif")
    if ws is not None:
        hr, headers = _linha_cabecalho(ws, "UF")
        if hr:
            band_por_col: Dict[int, str] = {}
            campo_por_col: Dict[int, str] = {}
            uf_col = reg_col = desc_col = None
            for c, h in headers.items():
                if not h:
                    continue
                nk = _norm(h)
                if nk == "uf":
                    uf_col = c
                elif nk in ("regiao", "regiaocodigo", "regiaozona"):
                    reg_col = c
                elif nk in ("descricao", "descricaorota", "rota"):
                    desc_col = c
                elif nk in ("advalorem", "advaloremcol", "advalorem%"):
                    campo_por_col[c] = "advalorem_pct"
                elif nk in ("freteval", "fretevalor", "freteval%"):
                    campo_por_col[c] = "frete_val_pct"
                elif nk in ("seccat", "seccatrs"):
                    campo_por_col[c] = "sec_cat"
                elif "pedagio" in nk:
                    campo_por_col[c] = "pedagio_col"
                elif "despacho" in nk:
                    campo_por_col[c] = "despacho"
                else:
                    bk = _col_para_banda(h)
                    if bk:
                        band_por_col[c] = bk
            for r in range(hr + 1, ws.max_row + 1):
                uf = ws.cell(row=r, column=uf_col).value if uf_col else None
                uf = str(uf).strip().upper() if uf else ""
                if len(uf) != 2 or not uf.isalpha():
                    continue
                linha = {
                    "uf": uf,
                    "regiao": str(ws.cell(row=r, column=reg_col).value or "").strip() if reg_col else "",
                    "descricao": str(ws.cell(row=r, column=desc_col).value or "").strip() if desc_col else "",
                    "faixas": {},
                }
                if _norm(linha["descricao"]) == "nan":
                    linha["descricao"] = ""
                for c, bk in band_por_col.items():
                    f = _to_float(ws.cell(row=r, column=c).value)
                    if f is not None and f > 0:
                        linha["faixas"][bk] = f
                for c, campo in campo_por_col.items():
                    f = _to_float(ws.cell(row=r, column=c).value)
                    if campo == "despacho":
                        if f is not None:      # vazio → None (usa o despacho padrão do acordo)
                            linha["despacho"] = f
                    else:
                        linha[campo] = f if f is not None else 0.0
                resultado["precos"].append(linha)

    # ── Prazos ──
    ws = _acha_aba(wb, "praz")
    if ws is not None:
        hr, headers = _linha_cabecalho(ws, "UF")
        if hr:
            col = {}
            for c, h in headers.items():
                if not h:
                    continue
                nk = _norm(h)
                if nk in ("uf", "ufdestino"):
                    col["uf"] = c
                elif nk in ("cidade", "cidadedestino", "municipio"):
                    col["cidade"] = c
                elif nk == "regiao":
                    col["regiao"] = c
                elif "cepinicial" in nk:
                    col["cep_inicial"] = c
                elif "cepfinal" in nk:
                    col["cep_final"] = c
                elif "prazo" in nk:
                    col["prazo_dias"] = c
            for r in range(hr + 1, ws.max_row + 1):
                uf = ws.cell(row=r, column=col["uf"]).value if "uf" in col else None
                cidade = ws.cell(row=r, column=col.get("cidade", 0)).value if "cidade" in col else None
                if not uf and not cidade:
                    continue
                prazo_raw = ws.cell(row=r, column=col["prazo_dias"]).value if "prazo_dias" in col else 0
                nums = re.findall(r"\d+", str(prazo_raw or ""))
                prazo = max(int(n) for n in nums) if nums else 0
                resultado["prazos"].append({
                    "uf": str(uf or "").strip().upper(),
                    "cidade": str(cidade or "").strip(),
                    "regiao": str(ws.cell(row=r, column=col["regiao"]).value or "").strip() if "regiao" in col else "",
                    "cep_inicial": ws.cell(row=r, column=col["cep_inicial"]).value if "cep_inicial" in col else None,
                    "cep_final": ws.cell(row=r, column=col["cep_final"]).value if "cep_final" in col else None,
                    "prazo_dias": prazo,
                })
    return resultado
