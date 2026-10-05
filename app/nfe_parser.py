"""
Parser de NF-e (Nota Fiscal eletrônica) — schema padrão SEFAZ v4.00.

Espelha a filosofia do `cte_parser`: a estrutura da NF-e (infNFe > ide/emit/dest/
det/total/transp) é federalmente padronizada, então a leitura busca por nome de
tag ignorando namespace e usa fallbacks em cascata em vez de caminhos fixos. Isso
torna o parser tolerante ao emissor (Olist/Tiny e qualquer outro ERP), já que o
que varia entre eles é só o preenchimento de campos livres, não o layout.

Alimenta o painel de Romaneio de Expedição: cada XML vira uma linha (uma NF) com
os dados necessários para montar o romaneio — número/chave, valor, peso, volumes,
destinatário/UF e a transportadora indicada na NF (transp/transporta).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as DET
from defusedxml.common import DefusedXmlException


@dataclass
class NfeData:
    arquivo: str
    chave: str
    numero: str
    serie: str
    data_emissao: Optional[str]   # dhEmi/dEmi como veio no XML (ISO)

    emitente_cnpj: str
    emitente_nome: str            # sua empresa (quem fatura no Olist)
    emitente_ie: str
    emitente_fone: str
    emitente_endereco: str        # logradouro, nº - bairro, município/UF - CEP
    emitente_municipio: str       # enderEmit/xMun — usado como "origem" na coleta
    emitente_uf: str

    dest_nome: str
    dest_doc: str                 # CNPJ ou CPF do destinatário
    dest_municipio: str
    dest_uf: str
    dest_cep: Optional[str]

    transportadora_cnpj: str
    transportadora_nome: str      # transp/transporta/xNome — pode vir vazio

    valor_nf: float               # total/ICMSTot/vNF
    peso_bruto: Optional[float]   # Σ transp/vol/pesoB
    peso_liquido: Optional[float] # Σ transp/vol/pesoL
    volumes: Optional[int]        # Σ transp/vol/qVol
    especie: str                  # transp/vol/esp (caixa/palete/…)

    # Produtos da nota (det/prod) — cada item: codigo, descricao, ncm, cfop,
    # unidade, quantidade, valor_unitario, valor_total. Vazio se não houver.
    itens: List[dict] = field(default_factory=list)

    # Finalidade (ide/finNFe): 1=Normal, 2=Complementar, 3=Ajuste, 4=DEVOLUÇÃO.
    finalidade: str = ""
    # Chave da NF-e de ORIGEM referenciada (ide/NFref/refNFe ou nas informações
    # complementares) — presente nas notas de devolução para amarrar à origem.
    ref_chave: str = ""


class NfeParseError(Exception):
    pass


def _strip_namespaces(elem: ET.Element) -> ET.Element:
    for el in elem.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return elem


def _clean_doc(v: Optional[str]) -> str:
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


def _chave_de_id(infnfe: ET.Element, root: ET.Element) -> str:
    """A chave de acesso (44 díg) fica no atributo Id do infNFe, prefixado 'NFe'."""
    chave = re.sub(r"\D", "", infnfe.get("Id", "") or "")
    if len(chave) == 44:
        return chave
    # Fallback: protNFe/infProt/chNFe (presente em nfeProc)
    ch = root.findtext(".//chNFe") or ""
    ch = re.sub(r"\D", "", ch)
    return ch if len(ch) == 44 else chave


def _numero_da_chave(chave: str) -> str:
    """nNF fica nas posições 26–34 da chave NF-e de 44 dígitos."""
    if len(chave) == 44:
        return chave[25:34].lstrip("0") or "0"
    return ""


def _serie_da_chave(chave: str) -> str:
    if len(chave) == 44:
        return chave[22:25].lstrip("0") or "0"
    return ""


def _formata_endereco(ender: Optional[ET.Element]) -> str:
    """Monta 'Logradouro, nº - Bairro, Município/UF - CEP' a partir de enderEmit."""
    if ender is None:
        return ""
    lgr = (ender.findtext("xLgr") or "").strip()
    nro = (ender.findtext("nro") or "").strip()
    bairro = (ender.findtext("xBairro") or "").strip()
    mun = (ender.findtext("xMun") or "").strip()
    uf = (ender.findtext("UF") or "").strip().upper()
    cep_raw = ender.findtext("CEP")
    cep = re.sub(r"\D", "", cep_raw) if cep_raw else ""
    cep_fmt = f"{cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep
    rua = lgr + (f", {nro}" if nro else "")
    if bairro:
        rua = f"{rua} - {bairro}" if rua else bairro
    cidade = f"{mun}/{uf}" if uf else mun
    partes = [p for p in (rua, cidade, cep_fmt) if p]
    return " - ".join(partes)


def _agrega_volumes(transp: Optional[ET.Element]):
    """Soma qVol/pesoB/pesoL de todos os <vol> (uma NF pode ter vários). Retorna
    (volumes, peso_bruto, peso_liquido, especie)."""
    if transp is None:
        return None, None, None, ""
    total_vol = 0
    total_pb = 0.0
    total_pl = 0.0
    tem_vol = tem_pb = tem_pl = False
    especie = ""
    for vol in transp.findall("vol"):
        q = _to_float(vol.findtext("qVol"))
        if q is not None:
            total_vol += int(q)
            tem_vol = True
        pb = _to_float(vol.findtext("pesoB"))
        if pb is not None:
            total_pb += pb
            tem_pb = True
        pl = _to_float(vol.findtext("pesoL"))
        if pl is not None:
            total_pl += pl
            tem_pl = True
        if not especie:
            especie = (vol.findtext("esp") or "").strip()
    peso_b = round(total_pb, 3) if tem_pb else None
    peso_l = round(total_pl, 3) if tem_pl else None
    # Olist/Tiny às vezes autoriza a NF com <pesoB> zerado (ou ausente) preenchendo
    # só o <pesoL>. Para o romaneio/frete o peso relevante é o BRUTO; quando ele não
    # veio (0 ou ausente), cai para o líquido, que carrega o peso real da carga.
    if peso_b in (None, 0, 0.0) and peso_l not in (None, 0, 0.0):
        peso_b = peso_l
    return (
        total_vol if tem_vol else None,
        peso_b,
        peso_l,
        especie,
    )


def _extrai_itens(infnfe: ET.Element) -> List[dict]:
    """Lê os produtos (det/prod) da NF-e. Tolerante: item sem <prod> é ignorado."""
    itens: List[dict] = []
    for det in infnfe.findall("det"):
        prod = det.find("prod")
        if prod is None:
            continue
        itens.append({
            "codigo": (prod.findtext("cProd") or "").strip(),
            "descricao": (prod.findtext("xProd") or "").strip(),
            "ncm": (prod.findtext("NCM") or "").strip(),
            "cfop": (prod.findtext("CFOP") or "").strip(),
            "unidade": (prod.findtext("uCom") or "").strip(),
            "quantidade": _to_float(prod.findtext("qCom")),
            "valor_unitario": _to_float(prod.findtext("vUnCom")),
            "valor_total": _to_float(prod.findtext("vProd")),
        })
    return itens


def parse_nfe_bytes(data: bytes, filename: str = "") -> NfeData:
    try:
        # defusedxml recusa DOCTYPE/entidades (proteção contra "billion laughs" e XXE)
        root = DET.fromstring(data)
    except (ET.ParseError, DefusedXmlException) as e:
        raise NfeParseError(f"XML inválido ou não permitido: {e}") from e

    root = _strip_namespaces(root)
    infnfe = root.find(".//infNFe")
    if infnfe is None:
        raise NfeParseError("Tag <infNFe> não encontrada — não parece ser uma NF-e válida.")

    chave = _chave_de_id(infnfe, root)

    ide = infnfe.find("ide")
    numero = (_find_text(ide, "nNF") or _numero_da_chave(chave) or "").strip()
    serie = (_find_text(ide, "serie") or _serie_da_chave(chave) or "").strip()
    data_emissao = _find_text(ide, "dhEmi") or _find_text(ide, "dEmi")

    # Finalidade + NF-e de origem referenciada (para detectar devolução).
    finalidade = (_find_text(ide, "finNFe") or "").strip()
    ref_chave = ""
    nfref = ide.find("NFref") if ide is not None else None
    if nfref is not None:
        ref_chave = re.sub(r"\D", "", _find_text(nfref, "refNFe") or "")
    if not ref_chave:  # fallback: chave de 44 dígitos nas informações complementares
        infcpl = _find_text(infnfe.find("infAdic"), "infCpl") or ""
        m = re.search(r"\b(\d{44})\b", infcpl)
        if m:
            ref_chave = m.group(1)

    emit = infnfe.find("emit")
    emit_cnpj = _clean_doc(_find_text(emit, "CNPJ") or _find_text(emit, "CPF"))
    emit_nome = (_find_text(emit, "xNome") or "").strip()
    emit_ie = (_find_text(emit, "IE") or "").strip()
    ender_emit = emit.find("enderEmit") if emit is not None else None
    emit_fone = (_find_text(ender_emit, "fone") or "").strip()
    emit_endereco = _formata_endereco(ender_emit)
    emit_municipio = (_find_text(ender_emit, "xMun") or "").strip()
    emit_uf = (_find_text(ender_emit, "UF") or "").strip().upper()

    dest = infnfe.find("dest")
    dest_nome = (_find_text(dest, "xNome") or "").strip()
    dest_doc = _clean_doc(_find_text(dest, "CNPJ") or _find_text(dest, "CPF"))
    ender_dest = dest.find("enderDest") if dest is not None else None
    dest_mun = (_find_text(ender_dest, "xMun") or "").strip()
    dest_uf = (_find_text(ender_dest, "UF") or "").strip().upper()
    dest_cep_raw = _find_text(ender_dest, "CEP")
    dest_cep = re.sub(r"\D", "", dest_cep_raw).zfill(8) if dest_cep_raw else None

    total = infnfe.find(".//total/ICMSTot")
    valor_nf = _to_float(_find_text(total, "vNF"))
    if valor_nf is None:
        # Fallback raríssimo: soma dos produtos
        valor_nf = _to_float(_find_text(total, "vProd")) or 0.0

    transp = infnfe.find("transp")
    transporta = transp.find("transporta") if transp is not None else None
    transp_cnpj = _clean_doc(_find_text(transporta, "CNPJ") or _find_text(transporta, "CPF"))
    transp_nome = (_find_text(transporta, "xNome") or "").strip()

    volumes, peso_b, peso_l, especie = _agrega_volumes(transp)
    itens = _extrai_itens(infnfe)

    return NfeData(
        arquivo=filename,
        chave=chave,
        numero=numero,
        serie=serie,
        data_emissao=data_emissao,
        emitente_cnpj=emit_cnpj,
        emitente_nome=emit_nome,
        emitente_ie=emit_ie,
        emitente_fone=emit_fone,
        emitente_endereco=emit_endereco,
        emitente_municipio=emit_municipio,
        emitente_uf=emit_uf,
        dest_nome=dest_nome,
        dest_doc=dest_doc,
        dest_municipio=dest_mun,
        dest_uf=dest_uf,
        dest_cep=dest_cep,
        transportadora_cnpj=transp_cnpj,
        transportadora_nome=transp_nome,
        valor_nf=valor_nf,
        peso_bruto=peso_b,
        peso_liquido=peso_l,
        volumes=volumes,
        especie=especie,
        itens=itens,
        finalidade=finalidade,
        ref_chave=ref_chave,
    )


def parse_many(files: List[tuple]) -> tuple:
    """files: lista de (filename, bytes). Retorna (notas_ok, erros)."""
    notas: List[NfeData] = []
    erros: List[dict] = []
    for filename, data in files:
        try:
            notas.append(parse_nfe_bytes(data, filename))
        except NfeParseError as e:
            erros.append({"arquivo": filename, "erro": str(e)})
    return notas, erros
