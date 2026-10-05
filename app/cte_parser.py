"""
Parser de CTe (Conhecimento de Transporte Eletrônico) — schema padrão SEFAZ v4.00.

A estrutura (ide, emit, rem/dest, exped/receb, vPrest, infCTeNorm/infCarga) é
federalmente padronizada e igual para qualquer transportadora emissora — o que
varia entre emissores é só o preenchimento de campos livres (xObs/ObsCont) e
quais tags opcionais aparecem. Por isso a leitura busca por nome de tag
ignorando namespace, e usa fallbacks em cascata (ex: exped -> rem) em vez de
depender de um único caminho fixo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException


@dataclass
class Componente:
    nome: str
    valor: float


@dataclass
class NotaFiscal:
    """NF-e transportada pelo CTe. Os campos cnpj_emitente/serie/numero são
    decompostos da chave de acesso (44 dígitos) e servem de elo com os arquivos
    EDI OCOREN, que referenciam a NF por (CNPJ emissor, série, número)."""
    chave: str
    cnpj_emitente: str
    serie: str
    numero: str


@dataclass
class CteData:
    arquivo: str
    chave: str
    numero: Optional[str]
    serie: Optional[str]
    data_emissao: Optional[str]

    transportadora_cnpj: str
    transportadora_nome: str

    origem_municipio: str
    origem_uf: str
    origem_cep: Optional[str]

    destino_municipio: str
    destino_uf: str
    destino_cep: Optional[str]
    destinatario_cnpj: str  # CNPJ/CPF do destinatário (p/ taxas por recebedor, ex.: TDE)
    remetente_cnpj: str  # CNPJ/CPF do remetente — numa devolução é o CLIENTE (a carga volta dele pra nós)

    peso_kg: Optional[float]
    peso_origem: str  # qual tpMed foi usado (transparência p/ o usuário)
    valor_mercadoria: Optional[float]

    valor_cobrado: float
    componentes: List[Componente] = field(default_factory=list)

    observacoes: str = ""
    is_spot_declarado: bool = False  # heurística: CTe já indica "TABELA: INFORMADO"
    caracteristica_adicional: str = ""  # compl/xCaracAd (ex.: "DEVOLUCAO", "REENTREGA", "REVERSA")
    tp_cte: str = "0"          # ide/tpCTe: 0=Normal, 1=Complementar, 2=Anulação, 3=Substituto
    tp_serv: str = "0"         # ide/tpServ: 0=Normal, 1=Subcontratação, 2=Redespacho, 3=Redesp. Interm.
    cte_comp_chave: str = ""   # infCteComp/chCTe — CT-e de origem que este complementa (tpCTe=1)
    notas: List[NotaFiscal] = field(default_factory=list)
    peso_real: Optional[float] = None  # peso físico (real/declarado/aferido), p/ verificar o cubado
    m3: Optional[float] = None         # volume em m³ (metros cúbicos), base do peso cubado


class CteParseError(Exception):
    pass


def _strip_namespaces(elem: ET.Element) -> ET.Element:
    for el in elem.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return elem


def _clean_cnpj(v: Optional[str]) -> str:
    return re.sub(r"\D", "", v or "")


def _to_float(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _find_text(elem: Optional[ET.Element], path: str) -> Optional[str]:
    if elem is None:
        return None
    found = elem.find(path)
    return found.text if found is not None and found.text is not None else None


def _find_cep(infcte: ET.Element, *paths: str) -> Optional[str]:
    for path in paths:
        node = infcte.find(path)
        if node is not None:
            cep = node.findtext("CEP")
            if cep:
                return re.sub(r"\D", "", cep).zfill(8)
    return None


_PESO_PRIORITY = [
    "PESO BASE DE CALCULO",
    "PESO BASE CALCULO",
    "PESO CUBADO",
    "PESO REAL",
]


def _infq_candidatos(infcte: ET.Element) -> Dict[str, float]:
    """Todas as medidas do infCarga por tpMed (PESO BASE DE CALCULO, PESO REAL,
    M3, METROS CUBICOS, ...). Base de _extract_peso / _extract_peso_real / _extract_m3."""
    infcarga = infcte.find(".//infCarga")
    if infcarga is None:
        return {}
    candidates: Dict[str, float] = {}
    for infq in infcarga.findall("infQ"):
        tp = (infq.findtext("tpMed") or "").strip().upper()
        q = _to_float(infq.findtext("qCarga"))
        if q is not None and tp:
            candidates[tp] = q
    return candidates


def _extract_peso(infcte: ET.Element) -> tuple[Optional[float], str]:
    candidates = _infq_candidatos(infcte)
    for label in _PESO_PRIORITY:
        if label in candidates and candidates[label] > 0:
            return candidates[label], label
    for tp, q in candidates.items():
        if "PESO" in tp and q > 0:
            return q, tp
    return None, ""


# Peso físico (não a base de cálculo, que pode já vir cubada) — para verificar o
# peso cubado de forma independente na auditoria.
_PESO_REAL_PRIORITY = ["PESO REAL", "PESO DECLARADO", "PESO AFERIDO", "PESO BRUTO"]


def _extract_peso_real(infcte: ET.Element) -> Optional[float]:
    candidates = _infq_candidatos(infcte)
    for label in _PESO_REAL_PRIORITY:
        if candidates.get(label, 0) > 0:
            return candidates[label]
    return None


def _extract_m3(infcte: ET.Element) -> Optional[float]:
    candidates = _infq_candidatos(infcte)
    for label in ("M3", "METROS CUBICOS", "M³", "CUBAGEM M3", "CUBAGEM"):
        if candidates.get(label, 0) > 0:
            return candidates[label]
    return None


def _decompor_chave_nfe(chave: str) -> Optional[NotaFiscal]:
    """Chave de acesso NF-e (44 dígitos): cUF(2) AAMM(4) CNPJ(14) mod(2) serie(3) nNF(9) tpEmis(1) cNF(8) cDV(1)."""
    chave = re.sub(r"\D", "", chave or "")
    if len(chave) != 44:
        return None
    return NotaFiscal(
        chave=chave,
        cnpj_emitente=chave[6:20],
        serie=chave[22:25].lstrip("0") or "0",
        numero=chave[25:34].lstrip("0") or "0",
    )


def _extract_notas(infcte: ET.Element) -> List[NotaFiscal]:
    """NF-es do CTe: infCTeNorm/infDoc/infNFe/chave (padrão) com fallback para
    infNF (modelo antigo, sem chave — identifica por série/número apenas)."""
    notas: List[NotaFiscal] = []
    vistos: set = set()

    for infnfe in infcte.findall(".//infDoc/infNFe"):
        nf = _decompor_chave_nfe(infnfe.findtext("chave") or "")
        if nf and nf.chave not in vistos:
            vistos.add(nf.chave)
            notas.append(nf)

    for infnf in infcte.findall(".//infDoc/infNF"):
        serie = (infnf.findtext("serie") or "").strip().lstrip("0") or "0"
        numero = re.sub(r"\D", "", infnf.findtext("nDoc") or "").lstrip("0")
        if numero:
            key = f"NF::{serie}::{numero}"
            if key not in vistos:
                vistos.add(key)
                notas.append(NotaFiscal(chave="", cnpj_emitente="", serie=serie, numero=numero))

    return notas


def _extract_observacoes(infcte: ET.Element) -> str:
    compl = infcte.find("compl")
    if compl is None:
        return ""
    parts: List[str] = []
    xobs = compl.findtext("xObs")
    if xobs:
        parts.append(xobs)
    for oc in compl.findall("ObsCont"):
        texto = oc.findtext("xTexto")
        if texto:
            parts.append(texto)
    return " | ".join(parts)


def _extract_caracteristica(infcte: ET.Element) -> str:
    """compl/xCaracAd (e o sinônimo xCaracSer): sinalização estruturada do
    emissor pra características especiais do CT-e — "DEVOLUCAO", "REENTREGA",
    "REVERSA" — bem mais confiável que procurar palavra-chave no texto livre
    de xObs (que só algumas transportadoras preenchem de forma descritiva)."""
    compl = infcte.find("compl")
    if compl is None:
        return ""
    valor = compl.findtext("xCaracAd") or compl.findtext("xCaracSer") or ""
    return valor.strip().upper()


@dataclass
class EventoCancelamento:
    chave: str
    justificativa: str
    data_evento: Optional[str]
    protocolo: Optional[str]


def parse_evento_cancelamento(data: bytes) -> Optional[EventoCancelamento]:
    """Detecta um evento de cancelamento de CT-e (procEventoCTe/evCancCTe —
    schema SEFAZ, tpEvento 110111), bem diferente do CT-e em si (sem vPrest,
    sem rotas, sem NF). Chega pelo mesmo e-mail/pasta que os CT-es porque a
    transportadora reenvia o pacote completo quando cancela e reemite. Retorna
    None se não for esse tipo de evento (deixa pro parse_cte_bytes normal)."""
    try:
        root = DET.fromstring(data)
    except (ET.ParseError, DefusedXmlException):
        return None
    root = _strip_namespaces(root)
    inf_evento = root.find(".//infEvento")
    if inf_evento is None or root.find(".//evCancCTe") is None:
        return None
    chave = (inf_evento.findtext("chCTe") or "").strip()
    if not chave:
        return None
    just = inf_evento.findtext(".//evCancCTe/xJust") or ""
    return EventoCancelamento(
        chave=chave,
        justificativa=just.strip(),
        data_evento=inf_evento.findtext("dhEvento"),
        protocolo=inf_evento.findtext(".//evCancCTe/nProt"),
    )


def parse_cte_bytes(data: bytes, filename: str = "") -> CteData:
    try:
        # defusedxml recusa DOCTYPE/entidades (proteção contra "billion laughs" e XXE)
        root = DET.fromstring(data)
    except (ET.ParseError, DefusedXmlException) as e:
        raise CteParseError(f"XML inválido ou não permitido: {e}") from e

    root = _strip_namespaces(root)
    infcte = root.find(".//infCte")
    if infcte is None:
        raise CteParseError("Tag <infCte> não encontrada — não parece ser um CTe válido.")

    chave = infcte.get("Id", "") or ""
    chave = re.sub(r"^CTe", "", chave)
    if not chave:
        chave = root.findtext(".//chCTe") or ""

    ide = infcte.find("ide")
    numero = _find_text(ide, "nCT")
    serie = _find_text(ide, "serie")
    dh_emi = _find_text(ide, "dhEmi")

    origem_mun = _find_text(ide, "xMunIni") or ""
    origem_uf = _find_text(ide, "UFIni") or ""

    # Destino: o destinatário real (dest/enderDest) determina a tarifa/tabela —
    # tem prioridade sobre o "recebedor" (receb/enderReceb), que costuma ser um
    # hub/filial intermediária da transportadora (ex.: CTe entregue fisicamente
    # em Teresina/THE mas destinado a Alto Longa/PII2). xMunFim/UFFim (ide) é só
    # o município de fim da prestação — usado como último fallback.
    dest_ender = infcte.find("dest/enderDest")
    receb_ender = infcte.find("receb/enderReceb")
    destino_mun = (
        (_find_text(dest_ender, "xMun") if dest_ender is not None else "")
        or (_find_text(receb_ender, "xMun") if receb_ender is not None else "")
        or _find_text(ide, "xMunFim") or ""
    )
    destino_uf = (
        (_find_text(dest_ender, "UF") if dest_ender is not None else "")
        or (_find_text(receb_ender, "UF") if receb_ender is not None else "")
        or _find_text(ide, "UFFim") or ""
    )

    origem_cep = _find_cep(infcte, "exped/enderExped", "rem/enderReme")
    destino_cep = _find_cep(infcte, "dest/enderDest", "receb/enderReceb")

    emit = infcte.find("emit")
    cnpj = _clean_cnpj(_find_text(emit, "CNPJ"))
    nome_transportadora = _find_text(emit, "xNome") or ""

    # Destinatário (recebedor da carga) — usado para taxas por CNPJ (TDE).
    dest_el = infcte.find("dest")
    destinatario_cnpj = _clean_cnpj(
        (_find_text(dest_el, "CNPJ") or _find_text(dest_el, "CPF")) if dest_el is not None else ""
    )
    if not destinatario_cnpj:
        receb_el = infcte.find("receb")
        destinatario_cnpj = _clean_cnpj(
            (_find_text(receb_el, "CNPJ") or _find_text(receb_el, "CPF")) if receb_el is not None else ""
        )

    # Remetente — numa devolução é o CLIENTE (a carga volta dele pra nós); usado
    # como fallback pra achar o CT-e de ida quando a NF não bate (ver auditor.py).
    rem_el = infcte.find("rem")
    remetente_cnpj = _clean_cnpj(
        (_find_text(rem_el, "CNPJ") or _find_text(rem_el, "CPF")) if rem_el is not None else ""
    )

    vprest = infcte.find("vPrest")
    valor_cobrado = _to_float(_find_text(vprest, "vTPrest")) or 0.0
    componentes = []
    if vprest is not None:
        for comp in vprest.findall("Comp"):
            nome_c = comp.findtext("xNome") or ""
            valor_c = _to_float(comp.findtext("vComp")) or 0.0
            componentes.append(Componente(nome=nome_c, valor=valor_c))

    # ICMS não é um <Comp> do frete — vem no bloco de imposto (imp/ICMS/*/vICMS).
    # Adiciona como componente sintético para aparecer no comparativo da auditoria.
    imp = infcte.find("imp")
    if imp is not None:
        icms_el = imp.find("ICMS")
        vicms = 0.0
        if icms_el is not None:
            for grupo in list(icms_el):
                vicms = _to_float(grupo.findtext("vICMS")) or 0.0
                if vicms:
                    break
        if vicms:
            componentes.append(Componente(nome="ICMS", valor=vicms))

    infcarga = infcte.find(".//infCarga")
    valor_mercadoria = _to_float(_find_text(infcarga, "vCarga")) if infcarga is not None else None

    peso_kg, peso_origem = _extract_peso(infcte)
    peso_real = _extract_peso_real(infcte)
    m3 = _extract_m3(infcte)

    observacoes = _extract_observacoes(infcte)
    is_spot = bool(re.search(r"TABELA\s*:\s*INFORMADO", observacoes, re.IGNORECASE))

    tp_cte = (_find_text(ide, "tpCTe") or "0").strip() or "0"
    tp_serv = (_find_text(ide, "tpServ") or "0").strip() or "0"
    cte_comp = infcte.find("infCteComp")   # CT-e complementado (quando tpCTe=1)
    cte_comp_chave = re.sub(r"\D", "", (cte_comp.findtext("chCTe") or "")) if cte_comp is not None else ""

    return CteData(
        arquivo=filename,
        chave=chave,
        numero=numero,
        serie=serie,
        data_emissao=dh_emi,
        transportadora_cnpj=cnpj,
        transportadora_nome=nome_transportadora,
        origem_municipio=origem_mun,
        origem_uf=origem_uf,
        origem_cep=origem_cep,
        destino_municipio=destino_mun,
        destino_uf=destino_uf,
        destino_cep=destino_cep,
        destinatario_cnpj=destinatario_cnpj,
        remetente_cnpj=remetente_cnpj,
        peso_kg=peso_kg,
        peso_origem=peso_origem,
        valor_mercadoria=valor_mercadoria,
        valor_cobrado=valor_cobrado,
        componentes=componentes,
        observacoes=observacoes,
        is_spot_declarado=is_spot,
        caracteristica_adicional=_extract_caracteristica(infcte),
        tp_cte=tp_cte,
        tp_serv=tp_serv,
        cte_comp_chave=cte_comp_chave,
        notas=_extract_notas(infcte),
        peso_real=peso_real,
        m3=m3,
    )
