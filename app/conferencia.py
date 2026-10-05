"""
Conferência de Fatura da Transportadora — fecha o ciclo financeiro do frete.

A transportadora manda, a cada quinzena/mês, uma fatura consolidada listando os
CTes que está cobrando. Este módulo confere CADA linha dessa fatura contra o
histórico de auditoria (app/history.py) em duas camadas:

  1. Fatura × CTe   — o valor que a transportadora cobra bate com o valor do
                      próprio CTe (vTPrest lido do XML)?
  2. CTe × Tabela   — aquele CTe já tinha sido auditado como OK ou como
                      DIVERGENTE do acordo (valor de tabela)?

Disso saem 4 situações por linha (ver STATUS_LABEL):

  CONFERE         fatura = CTe = tabela (dentro da tolerância) → liberado a pagar
  DIVERGE_CTE     fatura ≠ CTe          → a transportadora faturou valor diferente
                                          do que o próprio CTe declara
  CTE_DIVERGENTE  fatura = CTe, mas o CTe diverge da tabela → cobrança acima
                                          (ou abaixo) do contratado
  NAO_AUDITADO    o CTe da fatura não está no histórico → estão cobrando um
                                          documento que nunca passou pela auditoria

A fatura (cabeçalho + linhas) é persistida em SQLite próprio, mas a CONCILIAÇÃO
é recalculada a cada leitura contra o histórico atual: se o XML que faltava for
auditado depois, a linha 'NAO_AUDITADO' vira 'CONFERE' sozinha. O que se guarda
da fatura é só o que ela traz de novo (chave/número/valor cobrado); o resto
vem sempre do histórico vivo.
"""
from __future__ import annotations

import io
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app import history as audit_history

DB_PATH = os.getenv("CONFERENCIA_DB", str(Path(__file__).parent.parent / "tables" / "conferencia.db"))

# A fatura deve reproduzir o valor do próprio CTe; diferença só de centavos
# (arredondamento) ainda "confere". Acima disso, a transportadora re-precificou.
TOL_ABS = float(os.getenv("CONFERENCIA_TOL_ABS", "0.05"))

STATUS_LABEL = {
    "CONFERE": "Confere",
    "DIVERGE_CTE": "Fatura ≠ CTe",
    "CTE_DIVERGENTE": "CTe não aprovado (auditoria)",
    "NAO_AUDITADO": "Não auditado",
}

# A Auditoria tem seu PRÓPRIO ciclo de aprovação (history.aprovacao_status): CTe que
# bate com a tabela é aprovado automaticamente (AUTOMATICA); divergente fica PENDENTE
# até o auditor decidir — pode virar APROVADO_MANUAL (aceita a divergência) ou
# REPROVADO_MANUAL. A Conferência de Fatura LIBERA o pagamento quando o CTe está
# aprovado na auditoria (automático ou manual) — não pelo status cru OK/DIVERGENTE.
_AUDIT_APROVADO = {"AUTOMATICA", "APROVADO_MANUAL"}
_APROVACAO_LABEL = {
    "AUTOMATICA": "Aprovado (auto)",
    "APROVADO_MANUAL": "Aprovado (manual)",
    "PENDENTE": "Pendente na auditoria",
    "REPROVADO_MANUAL": "Reprovado na auditoria",
}


def _rotulo_auditoria(l: Dict) -> str:
    """Rótulo da coluna 'Auditoria' — mostra a DECISÃO de aprovação (mais relevante
    p/ a conferência) e cai no status cru só se não houver aprovação registrada."""
    ap = _APROVACAO_LABEL.get(l.get("aprovacao_auditoria"))
    if ap:
        return ap
    return audit_history._STATUS_LABEL.get(l.get("status_auditoria"), l.get("status_auditoria") or "—")

# Ciclo de vida da fatura (o operador avança manualmente).
STATUS_FATURA = ("aberta", "conferida", "aprovada", "contestada", "paga")


# ─── Banco ──────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS faturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transportadora TEXT,
            numero_fatura TEXT,
            competencia TEXT,
            arquivo TEXT,
            observacoes TEXT DEFAULT '',
            status TEXT DEFAULT 'aberta',
            criada_em TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fatura_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fatura_id INTEGER NOT NULL REFERENCES faturas(id) ON DELETE CASCADE,
            chave_cte TEXT,
            numero_cte TEXT,
            valor_fatura REAL,
            descricao TEXT,
            linha INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_itens_fatura ON fatura_itens(fatura_id)")
    conn.commit()
    conn.close()


# ─── Parsing de valores e chave ──────────────────────────────────────────────

def parse_money(raw) -> Optional[float]:
    """Aceita '1.234,56', '1234.56', 'R$ 1.234,56', 1234.5, ''. Retorna float ou None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"[^\d,.\-]", "", s)  # tira R$, espaços, etc.
    if not s or s in ("-", ".", ","):
        return None
    # Se tem os dois separadores, o último é o decimal (pt-BR: 1.234,56 / en: 1,234.56).
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _so_digitos(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, float):
        # 44 dígitos não cabem em float; openpyxl devolveria notação científica.
        raw = format(int(raw), "d") if raw.is_integer() else str(raw)
    return re.sub(r"\D", "", str(raw))


def normaliza_chave(raw) -> str:
    """Chave de acesso do CTe: 44 dígitos. Retorna a chave ou '' se não for válida."""
    d = _so_digitos(raw)
    return d if len(d) == 44 else ""


def numero_da_chave(chave: str) -> str:
    """nCT (número do CTe) fica nas posições 26–34 da chave de acesso de 44 dígitos."""
    if len(chave) == 44:
        return chave[25:34].lstrip("0") or "0"
    return ""


def normaliza_numero(raw) -> str:
    d = _so_digitos(raw)
    return d.lstrip("0") or ("0" if d else "")


# ─── Importador tolerante de fatura (XLSX / CSV) ─────────────────────────────

# Palavras-chave por tipo de coluna (comparadas em minúsculas e SEM acento — "Número"
# vira "numero"). Termos mais específicos primeiro (pontuam mais em _casa_header).
_HDR_CHAVE = ("chave de acesso", "chave cte", "chave", "acesso", "chave nfe")
_HDR_NUMERO = ("numero cte", "numero do conhecimento", "nro cte", "num cte", "n cte", "no cte",
               "conhecimento", "ctrc", "cte", "c-te", "documento",
               "numero", "nro", "num", "doc")
_HDR_VALOR = ("valor total", "valor do frete", "valor frete", "valor cte", "valor liquido",
              "valor a pagar", "liquido", "total da nota", "vl total", "vl frete", "vlr",
              "total", "valor", "frete", "a pagar", "prestacao")


def _sem_acento(s: str) -> str:
    tabela = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc")
    return s.translate(tabela)


def _norm_hdr(s) -> str:
    return _sem_acento(str(s or "").strip().lower())


def _casa_header(valor_norm: str, chaves: Tuple[str, ...]) -> int:
    """Pontua quão bem um cabeçalho casa com uma lista de termos (maior = melhor; 0 = não casa)."""
    melhor = 0
    for i, termo in enumerate(chaves):
        if termo in valor_norm:
            # termos no início da lista são mais específicos → pontuam mais
            melhor = max(melhor, len(chaves) - i + len(termo))
    return melhor


class FaturaParseError(Exception):
    pass


def _linhas_de_bytes(content: bytes, filename: str) -> List[List]:
    """Devolve a planilha como lista de linhas (cada linha = lista de células)."""
    nome = (filename or "").lower()
    # .xls (Excel 97-2003) é binário antigo — openpyxl não lê e o xlrd não está
    # instalado. Mensagem clara em vez do críptico "File is not a zip file".
    if nome.endswith(".xls") and not content[:2].startswith(b"PK"):
        raise FaturaParseError(
            "Arquivo .xls (Excel 97-2003) não é suportado. Abra no Excel e use "
            "\"Salvar como\" → \"Pasta de Trabalho do Excel (*.xlsx)\", depois importe o .xlsx."
        )
    # xlsx/xlsm (ou qualquer arquivo cujo conteúdo seja um zip do Office).
    if nome.endswith((".xlsx", ".xlsm")) or content[:2].startswith(b"PK"):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            linhas = [list(row) for row in ws.iter_rows(values_only=True)]
            wb.close()
            return linhas
        except FaturaParseError:
            raise
        except Exception as e:
            raise FaturaParseError(f"Não foi possível ler o Excel: {e}") from e
    # CSV / TXT
    for enc in ("utf-8-sig", "latin-1"):
        try:
            texto = content.decode(enc)
            break
        except UnicodeDecodeError:
            texto = None
    if texto is None:
        texto = content.decode("latin-1", errors="replace")
    import csv
    amostra = texto[:4096]
    delim = ";" if amostra.count(";") >= amostra.count(",") else ","
    return [list(row) for row in csv.reader(io.StringIO(texto), delimiter=delim)]


def _detecta_colunas(linhas: List[List]) -> Tuple[int, Dict[str, int]]:
    """Acha a linha de cabeçalho e mapeia col_chave/col_numero/col_valor.
    Retorna (indice_header, {'chave':i,'numero':i,'valor':i}); valores ausentes = -1."""
    for idx, linha in enumerate(linhas[:15]):  # cabeçalho costuma estar nas 1ªs linhas
        norm = [_norm_hdr(c) for c in linha]
        if not any(norm):
            continue
        col = {"chave": -1, "numero": -1, "valor": -1}
        score = {"chave": 0, "numero": 0, "valor": 0}
        for i, c in enumerate(norm):
            for tipo, chaves in (("chave", _HDR_CHAVE), ("numero", _HDR_NUMERO), ("valor", _HDR_VALOR)):
                s = _casa_header(c, chaves)
                if s > score[tipo]:
                    score[tipo] = s
                    col[tipo] = i
        # Cabeçalho válido = achou pelo menos um jeito de identificar o CTe e um valor.
        if col["valor"] >= 0 and (col["chave"] >= 0 or col["numero"] >= 0):
            return idx, col
    return -1, {"chave": -1, "numero": -1, "valor": -1}


def _detecta_por_conteudo(linhas: List[List]) -> Dict[str, int]:
    """Sem cabeçalho reconhecível: procura uma coluna com chaves de 44 dígitos e
    uma coluna com cara de valor monetário, olhando o conteúdo das linhas."""
    n_col = max((len(l) for l in linhas), default=0)
    chave_hits = [0] * n_col
    valor_hits = [0] * n_col
    for linha in linhas:
        for i in range(min(len(linha), n_col)):
            cel = linha[i]
            if normaliza_chave(cel):
                chave_hits[i] += 1
            elif parse_money(cel) is not None and _norm_hdr(cel):
                valor_hits[i] += 1
    col = {"chave": -1, "numero": -1, "valor": -1}
    if any(chave_hits):
        col["chave"] = max(range(n_col), key=lambda i: chave_hits[i])
    if any(valor_hits):
        # evita escolher a mesma coluna da chave
        cand = [i for i in range(n_col) if i != col["chave"]]
        if cand:
            col["valor"] = max(cand, key=lambda i: valor_hits[i])
    return col


def parse_fatura_bytes(content: bytes, filename: str = "") -> Dict:
    """Extrai as linhas de CTe da fatura. Retorna {'itens':[...], 'colunas':{...},
    'total_linhas', 'ignoradas'}. Cada item: {chave, numero, valor_fatura, descricao, linha}."""
    linhas = _linhas_de_bytes(content, filename)
    if not any(any(_norm_hdr(c) for c in l) for l in linhas):
        raise FaturaParseError("Arquivo vazio ou ilegível.")

    header_idx, col = _detecta_colunas(linhas)
    if header_idx < 0:
        col = _detecta_por_conteudo(linhas)
        header_idx = -1  # sem cabeçalho: lê tudo
        if col["valor"] < 0 or (col["chave"] < 0 and col["numero"] < 0):
            raise FaturaParseError(
                "Não consegui identificar as colunas de CTe e valor. Garanta que a fatura "
                "tenha um cabeçalho com colunas como 'Chave' (ou 'Nº CTe') e 'Valor'."
            )

    itens: List[Dict] = []
    ignoradas = 0
    inicio = header_idx + 1 if header_idx >= 0 else 0
    for n, linha in enumerate(linhas[inicio:], start=inicio + 1):
        def _cel(i):
            return linha[i] if 0 <= i < len(linha) else None

        chave = normaliza_chave(_cel(col["chave"])) if col["chave"] >= 0 else ""
        numero = normaliza_numero(_cel(col["numero"])) if col["numero"] >= 0 else ""
        if not numero and chave:
            numero = numero_da_chave(chave)
        valor = parse_money(_cel(col["valor"])) if col["valor"] >= 0 else None

        # Linha só vale se identifica um CTe (chave ou número) e tem valor.
        if (not chave and not numero) or valor is None:
            if any(_norm_hdr(c) for c in linha):
                ignoradas += 1
            continue

        descricao = ""
        if col["chave"] >= 0 and _cel(col["chave"]) and not chave:
            descricao = str(_cel(col["chave"])).strip()[:80]
        itens.append({
            "chave": chave,
            "numero": numero,
            "valor_fatura": round(valor, 2),
            "descricao": descricao,
            "linha": n,
        })

    if not itens:
        raise FaturaParseError(
            "Nenhuma linha de CTe com valor foi encontrada. Confira se o arquivo é a fatura "
            "da transportadora (uma linha por CTe, com chave/número e valor cobrado)."
        )
    return {"itens": itens, "colunas": col, "total_linhas": len(linhas), "ignoradas": ignoradas}


# ─── Importador DOCCOB (EDI PROCEDA — Documento de Cobrança de fretes) ───────
#
# DOCCOB é o padrão PROCEDA para a transportadora enviar a fatura/cobrança de
# fretes por EDI (irmão do OCOREN, que traz as ocorrências). É texto posicional:
#   000        header de comunicação
#   310 / 320  cabeçalho do documento de cobrança (fatura) — CNPJs, nº, total
#   311 / 321  DETALHE: um conhecimento (CTRC/CTe) cobrado — nº, série, data, valor
#   312/319…   notas fiscais / totalizador (ignorados aqui)
#
# Como cada transportadora tem pequenos desvios de layout, o parser tenta layouts
# candidatos e valida cada linha (nº só dígitos, valor > 0) — linha que não bate
# é rejeitada com motivo, nunca derruba o arquivo (mesma filosofia do OCOREN).
# ⚠ Os offsets abaixo são o layout PROCEDA padrão como PONTO DE PARTIDA; devem
# ser calibrados com UM arquivo real da transportadora (como foi feito no OCOREN).

_DOCCOB_DETALHE = {"311", "321"}
_DOCCOB_CABECALHO = {"310", "320"}
_DOCCOB_IGNORAR = {"000", "312", "322", "313", "323", "318", "319", "328", "329", "990", "999"}

# (offset_numero, tam_numero, offset_valor, tam_valor) — 0-based; valor com 2 casas
# decimais implícitas (centavos, zero-padded).
_DOCCOB_LAYOUTS = [
    (3, 9, 23, 15),    # nº(9) série(3) data(8) valor(15)
    (3, 12, 26, 15),   # nº(12) ...
    (17, 9, 37, 15),   # com CNPJ do pagador antes do nº
]


def _decode_edi(data: bytes) -> str:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _valor_edi(raw: str) -> Optional[float]:
    """Campo numérico PROCEDA: só dígitos, com 2 casas decimais implícitas (centavos)."""
    raw = raw.strip()
    if not raw.isdigit():
        return None
    v = int(raw) / 100.0
    return v if 0 < v < 1e8 else None


def _parse_doccob_detalhe(line: str) -> Tuple[Optional[Dict], str]:
    chave_m = re.search(r"\d{44}", line)
    chave = chave_m.group(0) if chave_m else ""
    for num_off, num_len, val_off, val_len in _DOCCOB_LAYOUTS:
        if len(line) < val_off + val_len:
            continue
        valor = _valor_edi(line[val_off:val_off + val_len])
        numero = normaliza_numero(line[num_off:num_off + num_len]) or (numero_da_chave(chave) if chave else "")
        if valor is not None and numero:
            return {"chave": chave, "numero": numero, "valor_fatura": round(valor, 2), "descricao": "", "linha": 0}, ""
    return None, "linha de detalhe (311/321) não bate com nenhum layout DOCCOB conhecido (número/valor)"


def parse_doccob_bytes(content: bytes, filename: str = "") -> Dict:
    """Lê um arquivo DOCCOB (EDI PROCEDA) e devolve a MESMA estrutura de itens do
    importador de planilha ({chave, numero, valor_fatura, ...}) + um 'cabecalho' com
    o que der para extrair da fatura (CNPJ/transportadora)."""
    text = _decode_edi(content)
    lines = [l.rstrip("\r\n") for l in text.splitlines()]
    if not any(l.strip() for l in lines):
        raise FaturaParseError("Arquivo DOCCOB vazio.")

    try:
        from app.carrier_mapping import get_carrier
    except Exception:
        get_carrier = lambda _c: None  # noqa: E731

    cabecalho = {"transportadora": "", "transportadora_cnpj": "", "numero_fatura": ""}
    itens: List[Dict] = []
    rejeitadas: List[Tuple[int, str]] = []
    conhecido = False
    total_reg = 0

    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        total_reg += 1
        rec = line[:3]
        if rec in _DOCCOB_CABECALHO:
            conhecido = True
            m = re.search(r"\d{14}", line[3:])
            if m and not cabecalho["transportadora_cnpj"]:
                cabecalho["transportadora_cnpj"] = m.group(0)
                try:
                    nome = get_carrier(m.group(0))
                    if nome:
                        cabecalho["transportadora"] = nome
                except Exception:
                    pass
        elif rec in _DOCCOB_DETALHE:
            conhecido = True
            item, motivo = _parse_doccob_detalhe(line)
            if item:
                item["linha"] = n
                itens.append(item)
            else:
                rejeitadas.append((n, motivo))
        elif rec in _DOCCOB_IGNORAR:
            conhecido = True
        else:
            rejeitadas.append((n, f"registro '{rec}' desconhecido — ignorado"))

    if not conhecido:
        raise FaturaParseError(
            "Nenhum registro PROCEDA (000/310/311…) encontrado — não parece ser um arquivo DOCCOB."
        )
    if not itens:
        raise FaturaParseError(
            "Nenhuma linha de conhecimento (registro 311/321) foi reconhecida. O layout DOCCOB "
            "desta transportadora precisa ser calibrado — envie um arquivo real para ajustar os campos."
        )
    return {"itens": itens, "cabecalho": cabecalho, "total_linhas": total_reg, "ignoradas": len(rejeitadas)}


# ─── Importador de fatura em PDF (boleto/fatura com tabela de CTes) ───────────
#
# Faturas de transportadora em PDF (ex.: layout SSW / ssw.inf.br, usado por TG
# Transportes e muitas outras) trazem um boleto + uma TABELA de conhecimentos:
#   CT-e | Data Emis | Destinatário | NF | Val mercad | Peso | Base | ICMS | ISS | ST | Frete
# Cada linha de detalhe começa com o nº do CTe seguido da data de emissão, e o
# ÚLTIMO valor monetário da linha é o Frete (o que a transportadora cobra).
# Extraímos (nº do CTe, frete) de cada linha; sem chave de 44 díg no PDF, o
# casamento com o histórico é por NÚMERO. ⚠ Calibrado no layout SSW; outra
# transportadora pode precisar de ajuste (mandar o modelo).

_MONEY_RE = r"\d{1,3}(?:\.\d{3})*,\d{2}"


def parse_pdf_bytes(content: bytes, filename: str = "") -> Dict:
    try:
        from pypdf import PdfReader
    except Exception:
        raise FaturaParseError("Leitura de PDF indisponível no servidor (biblioteca 'pypdf' ausente).")
    try:
        reader = PdfReader(io.BytesIO(content))
        texto = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        raise FaturaParseError(f"Não foi possível ler o PDF: {e}")

    linhas = texto.splitlines()

    # ── Cabeçalho da fatura ──
    cab = {"transportadora": "", "transportadora_cnpj": "", "numero_fatura": ""}
    m = re.search(r"Documento:?\s*(\d{5,8}-\d)", texto)
    if not m:
        m = re.search(r"FATURA\s*N[ºo°:.\s]*?(\d{5,8}-\d)", texto, re.IGNORECASE)
    if m:
        cab["numero_fatura"] = m.group(1)
    else:
        # Jamef (boleto BB + "Relação de Documentos Faturados"): "TITULO NR: 005632756"
        # ou "No.TÍTULO: 005632756". Não usa o formato NNNNN-N do SSW.
        mj = re.search(r"(?:T[IÍ]TULO\s*NR|N[o°]\.?\s*T[IÍ]TULO)[:.\s]*(\d{4,10})", texto, re.IGNORECASE)
        if mj:
            cab["numero_fatura"] = mj.group(1)
    # Transportadora: o 1º CNPJ do texto que casa com uma transportadora cadastrada —
    # pelo CNPJ completo ou, se não, pela RAIZ (8 primeiros dígitos), já que matriz e
    # filiais compartilham a raiz (ex.: fatura da matriz 0001, cadastro na filial).
    try:
        from app.carrier_mapping import get_carrier, load_mapping, SPOT_SENTINEL
        raiz2nome = {}
        for cnpj_m, nome_m in load_mapping().items():
            d = re.sub(r"\D", "", cnpj_m)
            if len(d) >= 8 and nome_m and nome_m != SPOT_SENTINEL:
                raiz2nome.setdefault(d[:8], nome_m)
    except Exception:
        get_carrier = lambda _c: None  # noqa: E731
        raiz2nome = {}
    for mc in re.finditer(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", texto):
        cnpj = re.sub(r"\D", "", mc.group(0))
        try:
            nome = get_carrier(cnpj)
        except Exception:
            nome = None
        if not nome:
            nome = raiz2nome.get(cnpj[:8])
        if nome:
            cab["transportadora"], cab["transportadora_cnpj"] = nome, cnpj
            break

    # ── Linhas de detalhe ──
    # Layout SSW (TG, FITLOG…): "<nº CTe> <data> ... <frete>".
    itens: List[Dict] = []
    for n, linha in enumerate(linhas, start=1):
        mrow = re.search(r"(\d{4,10})\s+(\d{2}/\d{2}/\d{2,4})", linha)
        if not mrow:
            continue
        moneys = re.findall(_MONEY_RE, linha)
        if not moneys:
            continue
        numero = normaliza_numero(mrow.group(1))
        frete = parse_money(moneys[-1])   # última coluna = Frete R$
        if not numero or frete is None or frete <= 0:
            continue
        itens.append({"chave": "", "numero": numero, "valor_fatura": round(frete, 2),
                      "descricao": "", "linha": n})

    # Layout Jamef ("Relação de Documentos Faturados"): a data vem PRIMEIRO, depois o
    # documento "<série>-<número>" (ex.: 14-004253299 → nCT 4253299) e o frete.
    if not itens:
        for n, linha in enumerate(linhas, start=1):
            mrow = re.search(r"\d{2}/\d{2}/\d{2,4}\s+\d{1,3}-(\d{3,12})\s+(" + _MONEY_RE + r")", linha)
            if not mrow:
                continue
            numero = normaliza_numero(mrow.group(1))   # dígitos após o hífen = nº do CTe
            frete = parse_money(mrow.group(2))
            if not numero or frete is None or frete <= 0:
                continue
            itens.append({"chave": "", "numero": numero, "valor_fatura": round(frete, 2),
                          "descricao": "", "linha": n})

    if not itens:
        raise FaturaParseError(
            "Nenhum CTe foi reconhecido na tabela do PDF. O layout desta fatura precisa ser "
            "calibrado — me envie o modelo para ajustar a leitura."
        )
    return {"itens": itens, "cabecalho": cab, "total_linhas": len(linhas), "ignoradas": 0}


def parse_auto(content: bytes, filename: str = "") -> Dict:
    """Ponto único de importação: detecta o tipo do arquivo e chama o parser certo —
    planilha (Excel/CSV) ou DOCCOB (EDI PROCEDA). Acrescenta 'formato' ao resultado.

    Assim o usuário pode soltar QUALQUER um dos formatos na mesma área de upload, sem
    ter de escolher o botão certo. O botão específico de DOCCOB continua existindo
    para forçar esse parser quando um arquivo texto for ambíguo."""
    nome = (filename or "").lower()
    # PDF (extensão ou magic bytes) → tabela de CTes do boleto/fatura.
    if nome.endswith(".pdf") or content[:4] == b"%PDF":
        return {"formato": "pdf", **parse_pdf_bytes(content, filename)}
    # Extensão de planilha (ou conteúdo que é um zip do Office) → Excel.
    if nome.endswith((".xlsx", ".xlsm", ".xls")) or content[:2].startswith(b"PK"):
        return {"formato": "planilha", **parse_fatura_bytes(content, filename)}
    # Arquivo-texto: pode ser DOCCOB posicional (PROCEDA) ou CSV/TXT tabular.
    try:
        head = _decode_edi(content)
    except Exception:
        head = ""
    primeira = next((l for l in head.splitlines() if l.strip()), "")
    if re.match(r"^(000|31\d|32\d|34\d|54\d)", primeira):   # registro PROCEDA no começo da 1ª linha
        return {"formato": "doccob", **parse_doccob_bytes(content, filename)}
    # Tenta como planilha/CSV; se não achar colunas, tenta DOCCOB como último recurso.
    try:
        return {"formato": "planilha", **parse_fatura_bytes(content, filename)}
    except FaturaParseError as e_plan:
        try:
            return {"formato": "doccob", **parse_doccob_bytes(content, filename)}
        except FaturaParseError:
            raise e_plan from None


# ─── Conciliação com o histórico de auditoria ────────────────────────────────

def _indices_historico() -> Tuple[Dict[str, Dict], Dict[str, List[Dict]]]:
    """Índices do histórico para casar itens da fatura: por chave_cte (única) e por
    número (pode repetir entre transportadoras — vira lista)."""
    rows = audit_history.query_history(limit=1000000)
    por_chave: Dict[str, Dict] = {}
    por_numero: Dict[str, List[Dict]] = {}
    for r in rows:
        ch = _so_digitos(r.get("chave_cte"))
        if len(ch) == 44:
            por_chave[ch] = r
        num = normaliza_numero(r.get("numero_cte"))
        if num:
            por_numero.setdefault(num, []).append(r)
    return por_chave, por_numero


def _casar_item(item: Dict, por_chave: Dict, por_numero: Dict, transportadora: Optional[str]) -> Optional[Dict]:
    """Acha o CTe do histórico correspondente ao item da fatura. Preferência: chave
    exata; senão número (desambiguando pela transportadora da fatura, se houver)."""
    ch = item.get("chave") or ""
    if ch and ch in por_chave:
        return por_chave[ch]
    num = item.get("numero") or ""
    candidatos = por_numero.get(num, [])
    if not candidatos:
        return None
    if len(candidatos) == 1:
        return candidatos[0]
    # Vários CTes com o mesmo número: tenta casar pela transportadora da fatura.
    if transportadora:
        alvo = transportadora.strip().lower()
        for c in candidatos:
            nome = (c.get("transportadora_tabela") or c.get("transportadora_nome_cte") or "").lower()
            if alvo and (alvo in nome or nome in alvo):
                return c
    return candidatos[0]


def _concilia_item(item: Dict, cte: Optional[Dict]) -> Dict:
    """Monta a linha conciliada (fatura × CTe × tabela) com status e valor contestável."""
    valor_fatura = item.get("valor_fatura") or 0.0
    linha = {
        "chave_cte": item.get("chave") or (cte or {}).get("chave_cte"),
        "numero_cte": item.get("numero") or (cte or {}).get("numero_cte"),
        "descricao": item.get("descricao") or "",
        "linha": item.get("linha"),
        "valor_fatura": round(valor_fatura, 2),
        "valor_cobrado": None,   # valor do próprio CTe (histórico)
        "valor_tabela": None,    # valor que a tabela previa (histórico)
        "transportadora": None,
        "destino": None,
        "data_emissao": None,
        "status_auditoria": None,
        "diff_fatura_cte": None,
        "diff_cte_tabela": None,
        "status": "NAO_AUDITADO",
        "status_auditoria": None,
        "aprovacao_auditoria": None,
        "contestavel": 0.0,
    }
    if cte is None:
        # Não está no histórico: bloqueia pagamento até auditar o XML.
        linha["contestavel"] = 0.0
        return linha

    vc = cte.get("valor_cobrado")
    linha["valor_cobrado"] = vc
    linha["valor_tabela"] = cte.get("valor_tabela")
    linha["transportadora"] = (cte.get("transportadora_tabela") or cte.get("transportadora_nome_cte") or "Desconhecida")
    linha["destino"] = cte.get("destino")
    linha["data_emissao"] = cte.get("data_emissao")
    linha["status_auditoria"] = cte.get("status")
    linha["aprovacao_auditoria"] = cte.get("aprovacao_status")

    if vc is not None:
        linha["diff_fatura_cte"] = round(valor_fatura - vc, 2)
    if cte.get("valor_tabela") is not None and vc is not None:
        linha["diff_cte_tabela"] = round(vc - cte["valor_tabela"], 2)

    # Camada 1: a fatura reproduz o valor do CTe?
    if vc is None or abs(valor_fatura - (vc or 0.0)) > TOL_ABS:
        linha["status"] = "DIVERGE_CTE"
        # O que se questiona é a diferença entre o que cobraram e o que o CTe diz.
        linha["contestavel"] = round(max(0.0, valor_fatura - (vc or 0.0)), 2)
        return linha

    # Camada 2: a fatura bate com o CTe. Libera se o CTe foi APROVADO na auditoria
    # (automático quando bate com a tabela, ou manual quando o auditor aceitou a
    # divergência). Pendente ou reprovado ainda segura o pagamento.
    if cte.get("aprovacao_status") not in _AUDIT_APROVADO:
        linha["status"] = "CTE_DIVERGENTE"
        linha["contestavel"] = round(max(0.0, (linha["diff_cte_tabela"] or 0.0)), 2)
        return linha

    linha["status"] = "CONFERE"
    return linha


def _resumo(linhas: List[Dict]) -> Dict:
    """Consolida os totais financeiros de uma fatura conciliada."""
    por_status = {k: 0 for k in STATUS_LABEL}
    valor_status = {k: 0.0 for k in STATUS_LABEL}
    total = 0.0
    contestavel = 0.0
    for l in linhas:
        s = l["status"]
        por_status[s] = por_status.get(s, 0) + 1
        valor_status[s] = valor_status.get(s, 0.0) + (l["valor_fatura"] or 0.0)
        total += l["valor_fatura"] or 0.0
        contestavel += l["contestavel"] or 0.0
    liberado = valor_status["CONFERE"]
    em_revisao = total - liberado
    return {
        "qtd_ctes": len(linhas),
        "total_fatura": round(total, 2),
        "valor_liberado": round(liberado, 2),
        "valor_em_revisao": round(em_revisao, 2),
        "valor_contestavel": round(contestavel, 2),
        "por_status": por_status,
        "valor_por_status": {k: round(v, 2) for k, v in valor_status.items()},
        "pct_liberado": round(liberado / total * 100, 1) if total else 0,
    }


def _conciliar(itens: List[Dict], transportadora: Optional[str]) -> List[Dict]:
    por_chave, por_numero = _indices_historico()
    linhas = []
    for item in itens:
        cte = _casar_item(item, por_chave, por_numero, transportadora)
        linhas.append(_concilia_item(item, cte))
    return linhas


def ctes_aprovados_auditoria(transportadora: Optional[str] = None, mes: Optional[str] = None) -> Dict:
    """Puxa da auditoria os CTes APROVADOS (status OK) para pré-montar uma fatura —
    o valor cobrado do próprio CTe vira o valor da linha. Filtra por transportadora
    (nome) e mês (competência) opcionais. Retorna itens no formato do importador."""
    rows = audit_history.query_history(mes=mes, transportadora=transportadora, status="OK", limit=1000000)
    itens: List[Dict] = []
    for i, r in enumerate(rows, start=1):
        # status OK não basta — devolução/reentrega pode estar OK e ainda PENDENTE
        # de aprovação humana (ver app.history.save_result); não pré-preenche fatura
        # com algo que a Camada 2 da conciliação (_concilia_item) ainda vai barrar.
        if r.get("aprovacao_status") not in _AUDIT_APROVADO:
            continue
        vc = r.get("valor_cobrado")
        if not vc:
            continue
        ch = _so_digitos(r.get("chave_cte"))
        itens.append({
            "chave": ch if len(ch) == 44 else "",
            "numero": normaliza_numero(r.get("numero_cte")),
            "valor_fatura": round(vc, 2),
            "descricao": "",
            "linha": i,
        })
    return {"itens": itens, "total": len(itens), "transportadora": transportadora or "", "mes": mes or ""}


# ─── CRUD de faturas ─────────────────────────────────────────────────────────

def _competencia_dos_itens(linhas: List[Dict]) -> Optional[str]:
    meses = sorted({(l.get("data_emissao") or "")[:7] for l in linhas if l.get("data_emissao")})
    if not meses:
        return None
    return meses[0] if len(meses) == 1 else f"{meses[0]} a {meses[-1]}"


def fatura_de_cte(chave_cte: str) -> Optional[Dict]:
    """Se esse CT-e já está lançado (linha de alguma fatura, qualquer status) —
    usado pelo auditor (app.auditor) pra travar reimportação de CT-e já
    faturado. Não filtra por status da fatura: mesmo 'aberta' já é lançamento."""
    ch = _so_digitos(chave_cte)
    if len(ch) != 44:
        return None
    conn = _get_conn()
    try:
        row = conn.execute("""
            SELECT f.id, f.numero_fatura, f.transportadora, f.status
            FROM fatura_itens i JOIN faturas f ON f.id = i.fatura_id
            WHERE i.chave_cte = ?
            LIMIT 1
        """, (ch,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_fatura(transportadora: str, numero_fatura: str, competencia: Optional[str],
                  arquivo: str, itens: List[Dict]) -> Dict:
    if not itens:
        raise ValueError("A fatura precisa de ao menos uma linha de CTe.")
    # Competência não informada: deriva do(s) mês(es) de emissão dos CTes conciliados.
    if not (competencia or "").strip():
        competencia = _competencia_dos_itens(_conciliar(itens, transportadora)) or ""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO faturas (transportadora, numero_fatura, competencia, arquivo, status, criada_em) "
        "VALUES (?,?,?,?,?,?)",
        (transportadora or "", numero_fatura or "", competencia or "", arquivo or "", "aberta", now),
    )
    fid = cur.lastrowid
    conn.executemany(
        "INSERT INTO fatura_itens (fatura_id, chave_cte, numero_cte, valor_fatura, descricao, linha) "
        "VALUES (?,?,?,?,?,?)",
        [(fid, it.get("chave") or None, it.get("numero") or None,
          it.get("valor_fatura"), it.get("descricao") or None, it.get("linha")) for it in itens],
    )
    conn.commit()
    conn.close()
    return get_fatura(fid)


def _fatura_row(conn, fatura_id: int):
    return conn.execute("SELECT * FROM faturas WHERE id = ?", (fatura_id,)).fetchone()


def get_fatura(fatura_id: int) -> Optional[Dict]:
    """Fatura com a conciliação recalculada contra o histórico atual."""
    conn = _get_conn()
    fat = _fatura_row(conn, fatura_id)
    if fat is None:
        conn.close()
        return None
    itens_raw = conn.execute(
        "SELECT chave_cte, numero_cte, valor_fatura, descricao, linha FROM fatura_itens "
        "WHERE fatura_id = ? ORDER BY linha", (fatura_id,)
    ).fetchall()
    conn.close()

    itens = [{"chave": r["chave_cte"] or "", "numero": r["numero_cte"] or "",
              "valor_fatura": r["valor_fatura"], "descricao": r["descricao"] or "",
              "linha": r["linha"]} for r in itens_raw]
    linhas = _conciliar(itens, fat["transportadora"])
    resumo = _resumo(linhas)
    bloq = blockers_aprovacao(linhas)
    return {
        "id": fat["id"],
        "transportadora": fat["transportadora"],
        "numero_fatura": fat["numero_fatura"],
        "competencia": fat["competencia"],
        "arquivo": fat["arquivo"],
        "observacoes": fat["observacoes"] or "",
        "status": fat["status"],
        "criada_em": fat["criada_em"],
        "resumo": resumo,
        "linhas": linhas,
        "nao_faturados": _nao_faturados(fat["transportadora"], fat["competencia"], linhas),
        "aprovavel": not bloq,
        "blockers": bloq,
    }


def _nao_faturados(transportadora: Optional[str], competencia: Optional[str], linhas: List[Dict]) -> List[Dict]:
    """Reverso: CTes auditados da(s) transportadora(s) desta fatura, no(s) mês(es) que
    ela cobre, que NÃO aparecem nela — possível cobrança pendente (ainda não faturada).

    O escopo de transportadora vem dos CTes que REALMENTE casaram na fatura, não do
    CNPJ/transportadora do cabeçalho — porque o emissor da fatura nem sempre é o mesmo
    do CTe (matriz vs. filial, redespacho, faturamento por 3º). Assim o reverso fica
    correto mesmo com CNPJ divergente e suporta fatura que junta mais de uma
    transportadora. Cai no rótulo informado só se nenhum CTe casou ainda."""
    nomes_fatura = {
        (l.get("transportadora") or "").strip().lower()
        for l in linhas
        if l.get("status") != "NAO_AUDITADO"
    }
    nomes_fatura = {n for n in nomes_fatura if n and n != "desconhecida"}
    if not nomes_fatura and transportadora:
        nomes_fatura = {transportadora.strip().lower()}
    if not nomes_fatura:
        return []
    # Os meses a checar são os que a fatura de fato cobre — os meses de emissão dos
    # CTes que ela lista. (A competência pode ser uma faixa "AAAA-MM a AAAA-MM", cujos
    # extremos não incluiriam os meses do meio.) Só cai na competência se nenhum CTe
    # da fatura tiver sido auditado ainda (todos NAO_AUDITADO, sem data de emissão).
    meses = sorted({(l.get("data_emissao") or "")[:7] for l in linhas if l.get("data_emissao")})
    if not meses:
        meses = re.findall(r"\d{4}-\d{2}", competencia or "")
    if not meses:
        return []
    chaves_fatura = {_so_digitos(l.get("chave_cte")) for l in linhas if l.get("chave_cte")}
    nums_fatura = {normaliza_numero(l.get("numero_cte")) for l in linhas if l.get("numero_cte")}
    faltantes = []
    vistos = set()
    for mes in meses:
        for r in audit_history.query_history(mes=mes, limit=1000000):
            if r.get("status") in ("ERRO_LEITURA", "SPOT", "TRANSPORTADORA_NAO_MAPEADA"):
                continue
            nome = (r.get("transportadora_tabela") or r.get("transportadora_nome_cte") or "").lower()
            if not any(alvo in nome or nome in alvo for alvo in nomes_fatura):
                continue
            ch = _so_digitos(r.get("chave_cte"))
            num = normaliza_numero(r.get("numero_cte"))
            if (ch and ch in chaves_fatura) or (num and num in nums_fatura):
                continue
            key = ch or f"num:{num}"
            if key in vistos:
                continue
            vistos.add(key)
            faltantes.append({
                "chave_cte": r.get("chave_cte"),
                "numero_cte": r.get("numero_cte"),
                "data_emissao": r.get("data_emissao"),
                "destino": r.get("destino"),
                "valor_cobrado": r.get("valor_cobrado"),
                "status_auditoria": r.get("status"),
                "aprovacao_auditoria": r.get("aprovacao_status"),
            })
    faltantes.sort(key=lambda x: x.get("data_emissao") or "")
    return faltantes


def alertas_conferencia() -> Dict:
    """Riscos de pagamento indevido cruzando TODAS as faturas:
      • duplicados: o mesmo CTe cobrado em mais de uma fatura (risco de pagar 2×);
      • orfaos: linha de fatura cujo CTe não está na auditoria (cobrado e ainda
        não reconhecido — CTe fantasma ou pendente de importação).
    """
    conn = _get_conn()
    try:
        itens = conn.execute(
            "SELECT fi.id, fi.fatura_id, fi.chave_cte, fi.numero_cte, fi.valor_fatura, "
            "f.numero_fatura, f.transportadora, f.competencia "
            "FROM fatura_itens fi JOIN faturas f ON f.id = fi.fatura_id"
        ).fetchall()
    finally:
        conn.close()

    # ── Duplicados: mesma identidade de CTe em faturas distintas ──
    grupos: Dict[str, Dict] = {}
    for it in itens:
        ch = _so_digitos(it["chave_cte"])
        num = normaliza_numero(it["numero_cte"])
        transp = (it["transportadora"] or "").strip().lower()
        chave_id = ch if len(ch) == 44 else (f"num:{num}|{transp}" if num else None)
        if not chave_id:
            continue
        g = grupos.setdefault(chave_id, {"numero_cte": it["numero_cte"], "chave_cte": it["chave_cte"],
                                         "ocorrencias": [], "faturas": set()})
        g["faturas"].add(it["fatura_id"])
        g["ocorrencias"].append({
            "fatura_id": it["fatura_id"], "numero_fatura": it["numero_fatura"],
            "transportadora": it["transportadora"], "competencia": it["competencia"],
            "valor_fatura": it["valor_fatura"] or 0.0,
        })
    duplicados = []
    for g in grupos.values():
        if len(g["faturas"]) >= 2:  # aparece em ≥2 faturas diferentes
            valor_repetido = round(sum(o["valor_fatura"] for o in g["ocorrencias"][1:]), 2)
            duplicados.append({
                "numero_cte": g["numero_cte"], "chave_cte": g["chave_cte"],
                "vezes": len(g["ocorrencias"]), "faturas": len(g["faturas"]),
                "valor_repetido": valor_repetido, "ocorrencias": g["ocorrencias"],
            })
    duplicados.sort(key=lambda d: -d["valor_repetido"])

    # ── Órfãos: CTe da fatura sem correspondência na auditoria ──
    por_chave, por_numero = _indices_historico()
    orfaos = []
    for it in itens:
        ch = _so_digitos(it["chave_cte"])
        item = {"chave": ch if len(ch) == 44 else "", "numero": normaliza_numero(it["numero_cte"])}
        if _casar_item(item, por_chave, por_numero, it["transportadora"]) is None:
            orfaos.append({
                "numero_cte": it["numero_cte"], "chave_cte": it["chave_cte"],
                "fatura_id": it["fatura_id"], "numero_fatura": it["numero_fatura"],
                "transportadora": it["transportadora"], "competencia": it["competencia"],
                "valor_fatura": it["valor_fatura"] or 0.0,
            })
    orfaos.sort(key=lambda o: -o["valor_fatura"])

    return {
        "duplicados": duplicados,
        "orfaos": orfaos,
        "valor_duplicado_total": round(sum(d["valor_repetido"] for d in duplicados), 2),
        "valor_orfao_total": round(sum(o["valor_fatura"] for o in orfaos), 2),
    }


def list_faturas() -> List[Dict]:
    """Lista de faturas com o resumo financeiro recalculado (mais recentes primeiro)."""
    conn = _get_conn()
    ids = [r["id"] for r in conn.execute("SELECT id FROM faturas ORDER BY criada_em DESC, id DESC").fetchall()]
    conn.close()
    out = []
    for fid in ids:
        f = get_fatura(fid)
        if f:
            out.append({k: f[k] for k in
                        ("id", "transportadora", "numero_fatura", "competencia",
                         "arquivo", "status", "criada_em", "resumo")})
    return out


# Status da fatura que significam "liberado o pagamento" — exigem que TODA linha
# esteja aprovável (importada + aprovada na auditoria + fatura batendo com o CTe).
_STATUS_QUE_EXIGEM_APROVACAO = {"aprovada", "paga"}


def linha_aprovavel(l: Dict) -> bool:
    """Libera pagamento quando a fatura reproduz o CTe E o CTe está aprovado na
    auditoria (automático ou manual) — ambas as condições já embutidas no status
    CONFERE (ver _concilia_item)."""
    return l.get("status") == "CONFERE"


def blockers_aprovacao(linhas: List[Dict]) -> List[Dict]:
    """Linhas que impedem aprovar a fatura, com o motivo de cada uma."""
    bloq = []
    for l in linhas:
        if linha_aprovavel(l):
            continue
        st = l.get("status")
        if st == "NAO_AUDITADO":
            motivo = "CTe ainda não importado/auditado"
        elif st == "DIVERGE_CTE":
            motivo = "valor da fatura diverge do CTe"
        else:  # CTE_DIVERGENTE: CTe não aprovado na auditoria (pendente/reprovado)
            ap = l.get("aprovacao_auditoria")
            if ap == "REPROVADO_MANUAL":
                motivo = "CTe reprovado na auditoria"
            else:
                motivo = "CTe pendente de aprovação na auditoria"
        bloq.append({"numero_cte": l.get("numero_cte"), "chave_cte": l.get("chave_cte"),
                     "status": st, "motivo": motivo})
    return bloq


class AprovacaoBloqueada(Exception):
    """Tentativa de aprovar/pagar uma fatura com CTes não aprováveis."""
    def __init__(self, status: str, blockers: List[Dict]):
        self.status = status
        self.blockers = blockers
        super().__init__(
            f"Não é possível marcar como '{status}': {len(blockers)} CTe(s) não estão importados "
            "e aprovados na auditoria (ou a fatura diverge do CTe). Resolva antes de aprovar."
        )


def update_fatura(fatura_id: int, status: Optional[str] = None, observacoes: Optional[str] = None,
                  numero_fatura: Optional[str] = None, competencia: Optional[str] = None,
                  transportadora: Optional[str] = None) -> Optional[Dict]:
    # Valida existência e a regra de aprovação ANTES de escrever (get_fatura recalcula
    # a conciliação contra o histórico vivo).
    atual = get_fatura(fatura_id)
    if atual is None:
        return None
    if status is not None:
        if status not in STATUS_FATURA:
            raise ValueError(f"Status inválido. Use um de: {', '.join(STATUS_FATURA)}.")
        if status in _STATUS_QUE_EXIGEM_APROVACAO:
            bloq = blockers_aprovacao(atual["linhas"])
            if bloq:
                raise AprovacaoBloqueada(status, bloq)
    if transportadora is not None and not transportadora.strip():
        raise ValueError("A transportadora não pode ficar em branco.")

    conn = _get_conn()
    if status is not None:
        conn.execute("UPDATE faturas SET status = ? WHERE id = ?", (status, fatura_id))
    if observacoes is not None:
        conn.execute("UPDATE faturas SET observacoes = ? WHERE id = ?", (observacoes, fatura_id))
    # Campos de cabeçalho editáveis (o operador pode ter esquecido o nº da fatura, ou
    # a transportadora/competência veio errada da leitura). Só grava o que veio no corpo.
    if numero_fatura is not None:
        conn.execute("UPDATE faturas SET numero_fatura = ? WHERE id = ?", (numero_fatura.strip(), fatura_id))
    if competencia is not None:
        conn.execute("UPDATE faturas SET competencia = ? WHERE id = ?", (competencia.strip(), fatura_id))
    if transportadora is not None:
        conn.execute("UPDATE faturas SET transportadora = ? WHERE id = ?", (transportadora.strip(), fatura_id))
    conn.commit()
    conn.close()
    return get_fatura(fatura_id)


def delete_fatura(fatura_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM faturas WHERE id = ?", (fatura_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ─── Exportação Excel ────────────────────────────────────────────────────────

_FILL = {
    "CONFERE": "C6EFCE",
    "DIVERGE_CTE": "FFC7CE",
    "CTE_DIVERGENTE": "FFEB9C",
    "NAO_AUDITADO": "D9D9D9",
}


def export_xlsx(fatura: Dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from app.history import _sanitize_cell

    header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    money_fmt = '"R$" #,##0.00'
    r = fatura["resumo"]

    wb = Workbook()
    ws = wb.active
    ws.title = "Conferência"
    ws["A1"] = "Conferência de Fatura da Transportadora"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"{fatura.get('transportadora') or '—'} · Fatura {fatura.get('numero_fatura') or '—'} · "
                f"{fatura.get('competencia') or '—'} — gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    resumo_linhas = [
        ("CTes na fatura", r["qtd_ctes"], None),
        ("Total faturado", r["total_fatura"], money_fmt),
        ("Liberado p/ pagamento", r["valor_liberado"], money_fmt),
        ("Em revisão", r["valor_em_revisao"], money_fmt),
        ("Valor contestável", r["valor_contestavel"], money_fmt),
        ("Confere", r["por_status"]["CONFERE"], None),
        ("Fatura ≠ CTe", r["por_status"]["DIVERGE_CTE"], None),
        ("CTe acima da tabela", r["por_status"]["CTE_DIVERGENTE"], None),
        ("Não auditado", r["por_status"]["NAO_AUDITADO"], None),
    ]
    for i, (label, valor, fmt) in enumerate(resumo_linhas, start=4):
        ws[f"A{i}"] = label
        ws[f"A{i}"].font = Font(bold=True)
        c = ws[f"B{i}"]
        c.value = valor
        if fmt and isinstance(valor, (int, float)):
            c.number_format = fmt
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 18

    # Detalhe linha a linha
    ws2 = wb.create_sheet("Linhas")
    headers = ["Situação", "Nº CTe", "Chave", "Transportadora", "Destino", "Emissão",
               "Valor fatura", "Valor CTe", "Valor tabela", "Fatura−CTe", "Contestável", "Auditoria"]
    for col_idx, label in enumerate(headers, start=1):
        c = ws2.cell(row=1, column=col_idx, value=label)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center")
    money_cols = {7, 8, 9, 10, 11}
    for row_idx, l in enumerate(fatura["linhas"], start=2):
        vals = [
            STATUS_LABEL.get(l["status"], l["status"]),
            l["numero_cte"], l["chave_cte"], l["transportadora"], l["destino"],
            (l["data_emissao"] or "")[:10], l["valor_fatura"], l["valor_cobrado"],
            l["valor_tabela"], l["diff_fatura_cte"], l["contestavel"],
            _rotulo_auditoria(l),
        ]
        for col_idx, val in enumerate(vals, start=1):
            c = ws2.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
            if col_idx in money_cols and isinstance(val, (int, float)):
                c.number_format = money_fmt
        fill = _FILL.get(l["status"])
        if fill:
            ws2.cell(row=row_idx, column=1).fill = PatternFill(start_color=fill, end_color=fill, fill_type="solid")
    for col_idx, w in enumerate([18, 11, 46, 22, 20, 12, 14, 14, 14, 12, 13, 18], start=1):
        ws2.column_dimensions[get_column_letter(col_idx)].width = w
    ws2.freeze_panes = "A2"

    # CTes não faturados (reverso)
    nf = fatura.get("nao_faturados") or []
    if nf:
        ws3 = wb.create_sheet("Não faturados")
        ws3["A1"] = "CTes auditados da transportadora ainda NÃO cobrados nesta fatura"
        ws3["A1"].font = Font(bold=True, size=12)
        heads = ["Nº CTe", "Chave", "Emissão", "Destino", "Valor CTe", "Auditoria"]
        for col_idx, label in enumerate(heads, start=1):
            c = ws3.cell(row=3, column=col_idx, value=label)
            c.fill = header_fill
            c.font = header_font
        for row_idx, l in enumerate(nf, start=4):
            vals = [l["numero_cte"], l["chave_cte"], (l["data_emissao"] or "")[:10],
                    l["destino"], l["valor_cobrado"], _rotulo_auditoria(l)]
            for col_idx, val in enumerate(vals, start=1):
                c = ws3.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
                if col_idx == 5 and isinstance(val, (int, float)):
                    c.number_format = money_fmt
        for col_idx, w in enumerate([11, 46, 12, 20, 14, 18], start=1):
            ws3.column_dimensions[get_column_letter(col_idx)].width = w
        ws3.freeze_panes = "A4"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ─── Relatório em PDF (fatura conferida/aprovada, para baixar) ────────────────

STATUS_FATURA_LABEL = {
    "aberta": "Aberta", "conferida": "Conferida", "aprovada": "Aprovada",
    "contestada": "Contestada", "paga": "Paga",
}
_STATUS_FATURA_COR = {
    "aberta": "#64748b", "conferida": "#3b82f6", "aprovada": "#2e7d32",
    "contestada": "#c62828", "paga": "#6aa626",
}


def _brl(v) -> str:
    """R$ no padrão pt-BR (1.234,56)."""
    if v is None:
        return "—"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def export_pdf(fatura: Dict) -> bytes:
    """Relatório limpo da fatura conferida (número, CTes, valores, situação) — para
    baixar/anexar ao pagamento. Foco na fatura APROVADA, mas serve a qualquer status."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

    ESCURO = colors.HexColor("#1f2937")
    CINZA = colors.HexColor("#6b7280")
    LINHA = colors.HexColor("#d7dbe3")
    ZEBRA = colors.HexColor("#f4f6f9")
    st_cor = colors.HexColor(_STATUS_FATURA_COR.get(fatura.get("status"), "#64748b"))

    styles = getSampleStyleSheet()
    p_meta = ParagraphStyle("meta", parent=styles["Normal"], fontSize=9, textColor=ESCURO, leading=13)
    p_metalbl = ParagraphStyle("metalbl", parent=styles["Normal"], fontSize=7.5, textColor=CINZA,
                               leading=10, spaceAfter=1)
    p_cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8.5, textColor=ESCURO, leading=11)
    p_cellr = ParagraphStyle("cellr", parent=p_cell, alignment=TA_RIGHT)

    r = fatura["resumo"]
    numero = fatura.get("numero_fatura") or "—"
    transp = fatura.get("transportadora") or "—"
    competencia = fatura.get("competencia") or "—"
    situacao = STATUS_FATURA_LABEL.get(fatura.get("status"), fatura.get("status") or "—")
    conferida = (fatura.get("criada_em") or "")[:10]
    gerado = datetime.now().strftime("%d/%m/%Y %H:%M")

    elems = []

    # Cabeçalho: marca + título + selo de situação
    cab = Table([[
        Paragraph('<font color="#1f2937"><b>Log</b></font><font color="#6aa626"><b>Hub</b></font>'
                  '<br/><font size="7" color="#6b7280">Relatórios Gerais de Logística</font>',
                  ParagraphStyle("brand", parent=styles["Normal"], fontSize=15, leading=17)),
        Paragraph(f'<font color="#ffffff"><b>{situacao.upper()}</b></font>',
                  ParagraphStyle("selo", parent=styles["Normal"], fontSize=11, alignment=TA_RIGHT, leading=14)),
    ]], colWidths=[120 * mm, 58 * mm])
    cab.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (1, 0), (1, 0), st_cor),
        ("TOPPADDING", (1, 0), (1, 0), 8), ("BOTTOMPADDING", (1, 0), (1, 0), 8),
        ("LEFTPADDING", (1, 0), (1, 0), 10), ("RIGHTPADDING", (1, 0), (1, 0), 10),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
    ]))
    elems.append(cab)
    elems.append(Spacer(1, 6 * mm))
    elems.append(Paragraph("Relatório de Conferência de Fatura",
                           ParagraphStyle("h1", parent=styles["Title"], fontSize=16, textColor=ESCURO,
                                          spaceAfter=2, alignment=0)))
    elems.append(Paragraph("Cada CTe cobrado foi conferido contra a auditoria de frete.",
                           ParagraphStyle("sub", parent=styles["Normal"], fontSize=8.5, textColor=CINZA, spaceAfter=8)))

    # Dados da fatura (grade 2 col)
    def _mc(lbl, val):
        return [Paragraph(lbl, p_metalbl), Paragraph(f"<b>{val}</b>", p_meta)]
    meta = Table([
        _mc("TRANSPORTADORA", transp) + _mc("Nº DA FATURA", numero),
        _mc("COMPETÊNCIA", competencia) + _mc("SITUAÇÃO", situacao),
        _mc("CONFERIDA EM", conferida or "—") + _mc("GERADO EM", gerado),
    ], colWidths=[45 * mm, 44 * mm, 45 * mm, 44 * mm])
    meta.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINHA),
    ]))
    elems.append(meta)
    elems.append(Spacer(1, 6 * mm))

    # Resumo financeiro (4 blocos)
    def _kpi(lbl, val, cor="#1f2937"):
        return Paragraph(f'<font size="7" color="#6b7280">{lbl}</font><br/>'
                         f'<font size="12" color="{cor}"><b>{val}</b></font>',
                         ParagraphStyle("kpi", parent=styles["Normal"], leading=15))
    kpis = Table([[
        _kpi("LIBERADO P/ PAGAMENTO", _brl(r["valor_liberado"]), "#2e7d32"),
        _kpi("TOTAL FATURADO", _brl(r["total_fatura"])),
        _kpi("EM REVISÃO", _brl(r["valor_em_revisao"]), "#b45309" if r["valor_em_revisao"] else "#1f2937"),
        _kpi("VALOR CONTESTÁVEL", _brl(r["valor_contestavel"]), "#c62828" if r["valor_contestavel"] else "#1f2937"),
    ]], colWidths=[44.5 * mm] * 4)
    kpis.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, LINHA), ("INNERGRID", (0, 0), (-1, -1), 0.5, LINHA),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elems.append(kpis)
    elems.append(Spacer(1, 6 * mm))

    # Tabela de CTes
    elems.append(Paragraph(f"CTes da fatura ({r['qtd_ctes']})",
                           ParagraphStyle("h2", parent=styles["Normal"], fontSize=10.5, textColor=ESCURO,
                                          spaceAfter=4, fontName="Helvetica-Bold")))
    head = ["#", "Nº CTe", "Emissão", "Destino", "Valor fatura", "Valor CTe", "Situação"]
    data = [head]
    for i, l in enumerate(fatura["linhas"], start=1):
        data.append([
            Paragraph(str(i), p_cell),
            Paragraph(str(l.get("numero_cte") or "—"), p_cell),
            Paragraph((l.get("data_emissao") or "")[:10] or "—", p_cell),
            Paragraph(str(l.get("destino") or "—"), p_cell),
            Paragraph(_brl(l.get("valor_fatura")), p_cellr),
            Paragraph(_brl(l.get("valor_cobrado")), p_cellr),
            Paragraph(STATUS_LABEL.get(l.get("status"), l.get("status") or "—"), p_cell),
        ])
    data.append([Paragraph("", p_cell), Paragraph("", p_cell), Paragraph("", p_cell),
                 Paragraph("<b>TOTAL</b>", p_cellr),
                 Paragraph(f"<b>{_brl(r['total_fatura'])}</b>", p_cellr),
                 Paragraph("", p_cell), Paragraph("", p_cell)])
    tbl = Table(data, colWidths=[8 * mm, 22 * mm, 20 * mm, 52 * mm, 26 * mm, 26 * mm, 24 * mm], repeatRows=1)
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), ESCURO),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("ALIGN", (4, 0), (5, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINHA),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, ESCURO),
        ("SPAN", (0, -1), (2, -1)),
    ]
    for ridx in range(1, len(data) - 1):
        if ridx % 2 == 0:
            estilo.append(("BACKGROUND", (0, ridx), (-1, ridx), ZEBRA))
    tbl.setStyle(TableStyle(estilo))
    elems.append(tbl)

    # Nota de aprovação / pendências
    elems.append(Spacer(1, 5 * mm))
    if fatura.get("aprovavel"):
        nota = ('<font color="#2e7d32"><b>✓ Fatura apta a pagamento.</b></font> '
                "Todos os CTes estão importados, aprovados na auditoria e a fatura reproduz o valor do CTe.")
    else:
        nb = len(fatura.get("blockers") or [])
        nota = (f'<font color="#c62828"><b>⚠ {nb} CTe(s) impedem a aprovação.</b></font> '
                "Há CTe não importado, divergente do CTe ou reprovado na auditoria — resolver antes de pagar.")
    elems.append(Paragraph(nota, ParagraphStyle("nota", parent=styles["Normal"], fontSize=8.5,
                                                textColor=ESCURO, leading=12)))
    if (fatura.get("observacoes") or "").strip():
        elems.append(Spacer(1, 3 * mm))
        elems.append(Paragraph(f'<font size="7" color="#6b7280">OBSERVAÇÕES</font><br/>{fatura["observacoes"]}',
                               ParagraphStyle("obs", parent=styles["Normal"], fontSize=8.5, textColor=ESCURO, leading=12)))

    def _rodape(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(CINZA)
        canvas.drawString(16 * mm, 10 * mm, f"LogHub · Conferência de Fatura · gerado em {gerado}")
        canvas.drawRightString(194 * mm, 10 * mm, f"Página {doc_.page}")
        canvas.setStrokeColor(LINHA)
        canvas.line(16 * mm, 13 * mm, 194 * mm, 13 * mm)
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=15 * mm, bottomMargin=18 * mm,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            title=f"Fatura {numero} — {transp}")
    doc.build(elems, onFirstPage=_rodape, onLaterPages=_rodape)
    return buf.getvalue()
