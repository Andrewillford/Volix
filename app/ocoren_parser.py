"""
Parser de arquivos EDI OCOREN (ocorrências de entrega) — padrão PROCEDA.

Suporta a versão 5.0 (registros 000/540/541/542/549) e, por compatibilidade,
a 3.1 (registros 000/340/341/342/349) — o layout do registro de ocorrência é
o mesmo, só muda o identificador.

O arquivo é texto posicional (largura fixa, registro de 250 chars). Layout
OFICIAL do registro 542 (OCOREN 5.0 — TIVIT/PROCEDA, posições 1-based):

  542 — Ocorrência na entrega da Nota Fiscal
    001-003  identificador do registro ("542")
    004-017  CNPJ do emissor da NF / embarcador (N14)
    018-020  série da NF (A3)
    021-029  número da NF (N9)
    030-032  código da ocorrência (N3)   ← 3 posições no 5.0
    033-040  data da ocorrência DDMMAAAA (N8)
    041-044  hora da ocorrência HHMM (N4)
    045-046  código de observação (N2)
    047-066  nº do romaneio / ordem de coleta (A20)
    067-...  campos SAP/conhecimento/datas de processo (estruturados)

O texto descritivo humano ("Chegada na unidade X…", etc.) NÃO fica no 542 —
vem no registro **543 (complemento)**, que segue cada 542. Este parser lê o
543 e anexa seu texto à ocorrência 542 anterior.

Também suporta a 3.1 (registros 000/340/341/342/349) e variações legadas
(código de 2 dígitos, número de 8), tentando layouts alternativos e validando
cada campo — linha que não bate em nenhum vira rejeição registrada (com
motivo), nunca derruba o arquivo inteiro.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

REGISTROS_OCORRENCIA = {"542", "342"}
REGISTROS_TRANSPORTADORA = {"541", "341"}
REGISTROS_HEADER_DOC = {"540", "340"}


@dataclass
class OcorrenciaEdi:
    cnpj_emissor: str      # CNPJ do emissor da NF (14 dígitos)
    serie: str             # série normalizada (sem zeros à esquerda)
    numero_nf: str         # número normalizado (sem zeros à esquerda)
    codigo: str            # código da ocorrência ("01", "02", …)
    data: Optional[str]    # ISO "YYYY-MM-DD"
    hora: Optional[str]    # "HH:MM"
    codigo_observacao: str
    texto: str
    linha: int             # nº da linha no arquivo (para diagnóstico)


@dataclass
class OcorenParseResult:
    arquivo: str
    transportadora_cnpj: str = ""
    transportadora_nome: str = ""
    versao: str = ""
    ocorrencias: List[OcorrenciaEdi] = field(default_factory=list)
    linhas_rejeitadas: List[Tuple[int, str]] = field(default_factory=list)
    total_linhas: int = 0


class OcorenParseError(Exception):
    pass


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _parse_data(raw: str) -> Optional[str]:
    """Aceita DDMMAAAA (padrão), AAAAMMDD e DDMMAA. Retorna ISO ou None."""
    raw = raw.strip()
    if not raw.isdigit():
        return None
    for fmt in ("%d%m%Y", "%Y%m%d", "%d%m%y"):
        if (fmt == "%d%m%y" and len(raw) != 6) or (fmt != "%d%m%y" and len(raw) != 8):
            continue
        try:
            d = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if 1990 <= d.year <= 2100:
            return d.isoformat()
    return None


def _parse_hora(raw: str) -> Optional[str]:
    raw = raw.strip()
    if len(raw) == 4 and raw.isdigit():
        hh, mm = int(raw[:2]), int(raw[2:])
        if hh < 24 and mm < 60:
            return f"{hh:02d}:{mm:02d}"
    return None


# Layouts candidatos para o miolo do registro 542 (a partir da 0-based col 17):
# (tam_serie, tam_numero, tam_codigo). O OFICIAL PROCEDA 5.0 é série 3 + número 9
# + código 3 — tentado PRIMEIRO. Os demais são tolerâncias p/ arquivos legados/3.1
# (código de 2 dígitos, número de 8, série curta).
_LAYOUTS = [
    (3, 9, 3),   # OFICIAL OCOREN 5.0 (série 3 + número 9 + código 3)
    (3, 9, 2),   # variação: número 9, código de 2 dígitos
    (3, 8, 2),   # legado 3.1: número 8 + código 2
    (2, 8, 2),   # série curta (arquivos muito antigos)
]


def _normalizar_codigo(codigo: str) -> str:
    """'001' -> '01' (casa com a tabela padrão); '170' continua '170'."""
    c = codigo.strip().lstrip("0")
    return (c or "0").zfill(2)


def _try_layout(line: str, tam_serie: int, tam_numero: int, tam_codigo: int) -> Optional[dict]:
    pos = 17 + tam_serie  # após "542" (3) + CNPJ (14) + série
    serie = line[17:pos]
    numero = line[pos:pos + tam_numero]
    pos += tam_numero
    codigo = line[pos:pos + tam_codigo]
    pos += tam_codigo
    data = _parse_data(line[pos:pos + 8])
    hora = _parse_hora(line[pos + 8:pos + 12])
    cod_obs = line[pos + 12:pos + 14]

    if not numero.strip().isdigit():
        return None
    if not codigo.strip().isdigit():
        return None
    if data is None:
        return None
    return {
        "serie": serie.strip().lstrip("0") or "0",
        "numero": numero.strip().lstrip("0"),
        "codigo": _normalizar_codigo(codigo),
        "data": data,
        "hora": hora,
        "cod_obs": cod_obs.strip(),
        # O 542 só tem campos posicionais — o texto descritivo vem do registro
        # 543 (complemento), anexado no laço de leitura logo após esta ocorrência.
        "texto": "",
    }


def _parse_542(line: str, n: int) -> Tuple[Optional[OcorrenciaEdi], str]:
    cnpj = re.sub(r"\D", "", line[3:17])
    if len(cnpj) != 14:
        return None, f"CNPJ do emissor da NF inválido: '{line[3:17].strip()}'"

    for tam_serie, tam_numero, tam_codigo in _LAYOUTS:
        campos = _try_layout(line, tam_serie, tam_numero, tam_codigo)
        if campos and campos["numero"]:
            return OcorrenciaEdi(
                cnpj_emissor=cnpj,
                serie=campos["serie"],
                numero_nf=campos["numero"],
                codigo=campos["codigo"],
                data=campos["data"],
                hora=campos["hora"],
                codigo_observacao=campos["cod_obs"],
                texto=campos["texto"],
                linha=n,
            ), ""

    return None, "linha não bate com nenhum layout conhecido do registro 542 (série/número/código/data)"


def parse_ocoren_bytes(data: bytes, filename: str = "") -> OcorenParseResult:
    text = _decode(data)
    result = OcorenParseResult(arquivo=filename)

    lines = [l.rstrip("\r\n") for l in text.splitlines()]
    if not any(l.strip() for l in lines):
        raise OcorenParseError("Arquivo vazio.")

    tem_registro_conhecido = False
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        result.total_linhas += 1
        rec = line[:3]

        if rec == "000":
            tem_registro_conhecido = True
        elif rec in REGISTROS_HEADER_DOC:
            tem_registro_conhecido = True
            result.versao = "5.0" if rec == "540" else "3.1"
        elif rec in REGISTROS_TRANSPORTADORA:
            tem_registro_conhecido = True
            cnpj = re.sub(r"\D", "", line[3:17])
            if len(cnpj) == 14:
                result.transportadora_cnpj = cnpj
            result.transportadora_nome = line[17:57].strip()
        elif rec in REGISTROS_OCORRENCIA:
            tem_registro_conhecido = True
            oc, motivo = _parse_542(line, n)
            if oc:
                result.ocorrencias.append(oc)
            else:
                result.linhas_rejeitadas.append((n, motivo))
        elif rec in ("543", "343"):
            # Complemento: texto descritivo livre da ocorrência anterior
            # (ex.: "Chegada na unidade BELO HORIZONTE em 25/08/26, 09:29h.").
            tem_registro_conhecido = True
            compl = line[3:].strip()
            if compl and result.ocorrencias:
                prev = result.ocorrencias[-1]
                prev.texto = f"{prev.texto} {compl}".strip() if prev.texto else compl
        elif rec in ("544", "344"):
            tem_registro_conhecido = True  # detalhe de mercadoria/item — não usamos
        elif rec in ("549", "349"):
            tem_registro_conhecido = True  # totalizador — não precisamos dele
        else:
            result.linhas_rejeitadas.append((n, f"registro '{rec}' desconhecido — ignorado"))

    if not tem_registro_conhecido:
        raise OcorenParseError(
            "Nenhum registro PROCEDA (000/540/541/542…) encontrado — não parece ser um arquivo OCOREN."
        )
    return result
