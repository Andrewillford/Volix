"""
Parser de RPS/NFS-e (Recibo Provisório de Serviços) em PDF — usado quando a
transportadora presta um serviço DENTRO do mesmo município (ex.: SERRA/ES ->
SERRA/ES): nesse caso não existe CT-e (documento estadual, ICMS), a prefeitura
exige NFS-e municipal (ISS) em vez dele, e a transportadora manda só o PDF do
RPS — não gera XML nenhum pra gente. Este módulo lê esse PDF e devolve a MESMA
estrutura do CT-e (app.cte_parser.CteData) pra reaproveitar o motor de
auditoria inteiro (app.auditor) sem duplicar nenhuma lógica de comparação.

O SSW (Fitlog/TG) gera esse RPS num layout fixo e previsível; o texto extraído
por pypdf em modo "layout" preserva alinhamento de coluna o suficiente pra
cada rótulo ficar na mesma linha do seu valor (ao contrário do modo padrão,
que separa TODOS os rótulos de TODOS os valores em blocos). Os regexes abaixo
foram calibrados num RPS real da TG (ver conversa) — se outra transportadora
emitir RPS num layout diferente, este parser precisa de ajuste.
"""
from __future__ import annotations

import re
from typing import Optional

from app.cte_parser import CteData, Componente, NotaFiscal, _decompor_chave_nfe

_VALID_UF = {
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA",
    "PB", "PE", "PI", "PR", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
}

_COMPONENTES_FRETE = (
    "FRETE PESO", "FRETE VALOR", "DESPACHO", "GRIS", "POS", "PEDAGIO", "TAS",
    "TDA", "TRT", "EMEX", "TDE", "IMP REPASSADO", "ADIC FRETE", "TEC",
)


class RpsParseError(Exception):
    pass


def is_rps_pdf(data: bytes) -> bool:
    """Reconhecimento leve (sem depender de exceção): só tenta ler como RPS
    se o PDF realmente tiver a marca do documento. Evita gastar um parse
    completo em PDF nenhuma-relação (ex.: fatura da transportadora)."""
    try:
        texto = _extrair_texto(data)
    except Exception:
        return False
    return bool(re.search(r"RECIBO PROVIS[ÓO]RIO DE SERVI[ÇC]OS", texto, re.IGNORECASE))


def _extrair_texto(data: bytes) -> str:
    from pypdf import PdfReader
    import io
    reader = PdfReader(io.BytesIO(data))
    if not reader.pages:
        raise RpsParseError("PDF sem páginas.")
    # extraction_mode="layout" preserva alinhamento de coluna (rótulo e valor
    # na mesma linha) — o modo padrão separa todos os rótulos dos valores.
    return reader.pages[0].extract_text(extraction_mode="layout") or ""


def _clean_cnpj(v: Optional[str]) -> str:
    return re.sub(r"\D", "", v or "")


def _to_float(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    v = v.strip().replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def _municipio_uf(raw: str) -> tuple[str, str]:
    """'SERRA/ES' ou 'SERRA - ES' -> ('SERRA', 'ES')."""
    m = re.match(r"^\s*(.+?)\s*[/\-]\s*([A-Z]{2})\s*$", raw.strip())
    if m and m.group(2).upper() in _VALID_UF:
        return m.group(1).strip().upper(), m.group(2).upper()
    return raw.strip().upper(), ""


def _bloco(texto: str, inicio_label: str, fim_labels: tuple[str, ...]) -> str:
    """Recorta o texto entre `inicio_label` e o próximo label de `fim_labels`
    (ou fim do texto) — usado pra isolar um bloco de endereço (REMETENTE,
    DESTINATARIO etc.) e buscar CNPJ/CEP só dentro dele, sem pegar o do
    bloco vizinho por engano."""
    ini = texto.find(inicio_label)
    if ini == -1:
        return ""
    ini += len(inicio_label)
    fim = len(texto)
    for lbl in fim_labels:
        pos = texto.find(lbl, ini)
        if pos != -1:
            fim = min(fim, pos)
    return texto[ini:fim]


def parse_rps_bytes(data: bytes, filename: str = "") -> CteData:
    texto = _extrair_texto(data)
    if not texto.strip():
        raise RpsParseError("Não foi possível extrair texto do PDF (pode ser um PDF escaneado/imagem).")
    if not re.search(r"RECIBO PROVIS[ÓO]RIO DE SERVI[ÇC]OS", texto, re.IGNORECASE):
        raise RpsParseError("PDF não parece ser um RPS/NFS-e (marca do documento não encontrada).")

    # Cabeçalho: nº RPS, emissão, nº NFS-e, emissão NFS-e — valores em sequência
    # numa linha só (formato bem específico, é o âncora mais confiável do doc).
    m_cab = re.search(
        r"(\d{3}-\d{9})\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})\s+"
        r"(\d{9,15})\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})",
        texto,
    )
    if not m_cab:
        raise RpsParseError("Cabeçalho do RPS (número/emissão) não encontrado — layout inesperado.")
    numero_rps = m_cab.group(1)
    data_emissao = f"{m_cab.group(2)[6:10]}-{m_cab.group(2)[3:5]}-{m_cab.group(2)[0:2]}T{m_cab.group(3)}"
    numero_nfse = m_cab.group(4)

    m_cnpj_transp = re.search(r"\bCNPJ\s+(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})\s+IM", texto)
    transportadora_cnpj = _clean_cnpj(m_cnpj_transp.group(1)) if m_cnpj_transp else ""
    primeira_linha = texto.strip().splitlines()[0] if texto.strip() else ""
    transportadora_nome = re.split(r"\s{2,}", primeira_linha.strip())[0].strip()

    m_rota = re.search(
        r"ORIGEM DA PRESTA[ÇC][ÃA]O\s+DESTINO DA PRESTA[ÇC][ÃA]O.*?\n"
        r"\s*(\S.*?/[A-Z]{2})\s{2,}(\S.*?/[A-Z]{2})",
        texto,
    )
    if m_rota:
        origem_municipio, origem_uf = _municipio_uf(m_rota.group(1))
        destino_municipio, destino_uf = _municipio_uf(m_rota.group(2))
    else:
        origem_municipio = origem_uf = destino_municipio = destino_uf = ""

    def _campo(label: str, padrao: str = r"([\d.,]+)") -> Optional[str]:
        m = re.search(rf"{label}\s+{padrao}", texto)
        return m.group(1) if m else None

    valor_mercadoria = _to_float(_campo(r"VALOR MERCADORIA \(R\$\)"))
    peso_kg = _to_float(_campo(r"PESO C[ÁA]LCULO \(Kg\)"))
    frete_total = _to_float(_campo(r"FRETE TOTAL \(R\$\)")) or 0.0

    componentes = []
    for nome in _COMPONENTES_FRETE:
        v = _campo(re.escape(nome))
        val = _to_float(v)
        if val:
            componentes.append(Componente(nome=nome, valor=val))

    # Destinatário: bloco entre "DESTINATARIO" e o próximo rótulo de endereço
    # (EXPEDIDOR) — pega CNPJ/CPF e CEP só dentro desse recorte.
    bloco_dest = _bloco(texto, "DESTINATARIO", ("EXPEDIDOR", "RECEBEDOR/LOC ENTREGA", "TOMADOR"))
    m_dest_doc = re.search(r"CNPJ\s+(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})", bloco_dest)
    destinatario_cnpj = _clean_cnpj(m_dest_doc.group(1)) if m_dest_doc else ""
    m_dest_cep = re.search(r"CEP\s+(\d{5}-?\d{3})", bloco_dest)
    destino_cep = _clean_cnpj(m_dest_cep.group(1)).zfill(8) if m_dest_cep else None

    bloco_rem = _bloco(texto, "REMETENTE", ("DESTINATARIO", "EXPEDIDOR"))
    m_rem_doc = re.search(r"CNPJ\s+(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})", bloco_rem)
    remetente_cnpj = _clean_cnpj(m_rem_doc.group(1)) if m_rem_doc else ""

    notas = []
    for chave in re.findall(r"NF-E:\s*(\d{44})", texto):
        nf = _decompor_chave_nfe(chave)
        if nf and nf.chave not in {n.chave for n in notas}:
            notas.append(nf)

    # Chave sintética (RPS não tem chave de acesso de 44 dígitos como o CT-e):
    # CNPJ do emissor + número da NFS-e é único o suficiente pra servir de PK
    # no histórico de auditoria e ser idempotente numa reimportação.
    chave = f"RPS{transportadora_cnpj}{numero_nfse}"

    return CteData(
        arquivo=filename,
        chave=chave,
        # A fatura da transportadora referencia o Nº NFS-e (confirmado num caso
        # real: item de fatura com numero_cte='6044' == final de
        # '000000000006044'), não o Nº RPS — usa o NFS-e aqui pra "Conferência
        # de Fatura" (app.conferencia) casar o item sozinha.
        numero=numero_nfse.lstrip("0") or numero_nfse,
        serie="RPS",
        data_emissao=data_emissao,
        transportadora_cnpj=transportadora_cnpj,
        transportadora_nome=transportadora_nome,
        origem_municipio=origem_municipio,
        origem_uf=origem_uf,
        origem_cep=None,
        destino_municipio=destino_municipio,
        destino_uf=destino_uf,
        destino_cep=destino_cep,
        destinatario_cnpj=destinatario_cnpj,
        remetente_cnpj=remetente_cnpj,
        peso_kg=peso_kg,
        peso_origem="PESO CALCULO (RPS)",
        valor_mercadoria=valor_mercadoria,
        valor_cobrado=frete_total,
        componentes=componentes,
        observacoes=f"RPS/NFS-e (serviço intramunicipal, sem CT-e) — nº RPS {numero_rps}",
        is_spot_declarado=False,
        caracteristica_adicional="",
        tp_cte="0",
        tp_serv="0",
        cte_comp_chave="",
        notas=notas,
    )
