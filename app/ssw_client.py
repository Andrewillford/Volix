"""
Cliente para a WebAPI de rastreamento da SSW (sistema usado por Fitlog e TG).

Cobre só o lado de RASTREAMENTO (equivalente ao OCOREN) — é uma chamada de
SAÍDA (nosso servidor pergunta pra SSW), então funciona rodando 100% local,
sem precisar de URL pública nem webhook (ao contrário do envio de XML CTe e do
envio de ocorrências da SSW, que são webhooks — a SSW precisaria alcançar um
endereço nosso).

Credenciais por transportadora, em .env:
  SSW_<CARRIER>_CNPJ   — CNPJ do remetente cadastrado no rastreamento da SSW
  SSW_<CARRIER>_SENHA  — senha de rastreamento fornecida pela transportadora
                         (a doc da SSW não deixa claro se é a mesma senha do
                         portal ou uma senha própria de rastreamento — a
                         confirmar com Fitlog/TG)

<CARRIER> = "FITLOG" ou "TG" (mesmas chaves usadas em app.carrier_mapping).

Documentação oficial: https://ssw.inf.br/ajuda/tracking.html — só especifica
os parâmetros de entrada; o formato da resposta não é documentado publicamente
(o exemplo da página só é populado ao vivo, com credenciais reais). Testado
contra a TG Transportes em 2026-09-02 — resposta real:

  {
    "success": bool, "message": str,
    "header": {"remetente": str, "destinatario": str, "comprovante"?: url},
    "tracking": [
      {"data_hora": iso, "data_hora_efetiva": iso, "dominio": str, "filial": str,
       "cidade": "CIDADE / UF", "ocorrencia": "TEXTO (CODIGO)",
       "codigo_ocorrencia": str, "descricao": str,
       "tipo": "Informativo" | "Entrega" | ...(outros ainda não observados),
       "nome_recebedor": str, "nro_doc_recebedor": str},
      ...
    ]
  }

Os códigos de ocorrência da SSW (codigo_ocorrencia) são uma numeração PRÓPRIA,
DIFERENTE da tabela PROCEDA usada pelo OCOREN por e-mail — ex.: código "80" no
OCOREN é "Extravio total" (sinistro), mas na SSW é só "CT-e emitido"
(informativo). Por isso `resposta_para_ocoren` grava o código com o prefixo
"SSW-" (ex.: "SSW-80") em tables/ocorrencias_codigos.json — nunca colide com
os códigos PROCEDA, e qualquer código novo ainda não catalogado cai com
segurança em OUTROS (mesmo comportamento padrão já existente pra código
desconhecido). Catalogados até 2026-09-02: entregue (01), local fechado/
ausente (21), devolução (36), sobra/sinistro (51), entregue no parceiro de
redespacho (57), emissão/trânsito (80/82/83/85/98) — cobre as 5 categorias
com impacto de negócio; qualquer código fora dessa lista cai em OUTROS.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

TRACKING_URL = "https://ssw.inf.br/api/tracking"


def _load_env() -> None:
    """Lê o .env na raiz do projeto pra variáveis SSW_* (mesmo padrão do
    app.email_ingest — autocontido, não depende da ordem de import)."""
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        return
    try:
        for linha in env_file.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            chave = chave.strip()
            valor = valor.strip().strip('"').strip("'")
            if chave.startswith("SSW_") and chave not in os.environ:
                os.environ[chave] = valor
    except OSError as e:
        logger.warning(f"Falha ao ler .env: {e}")


_load_env()


def _get(carrier: str, campo: str) -> str:
    return (os.getenv(f"SSW_{carrier.upper()}_{campo}", "") or "").strip()


def is_configured(carrier: str) -> bool:
    # senha é opcional na WebAPI de rastreamento — só fica obrigatória se o
    # cadastro do cliente na transportadora marcar como obrigatória (a Fitlog
    # não marca; a TG, por segurança, mantemos exigindo já que funciona com ela).
    return bool(_get(carrier, "CNPJ"))


class SswTrackingError(Exception):
    pass


async def consultar_rastreamento(
    carrier: str,
    *,
    nro_nf: Optional[int] = None,
    pedido: Optional[str] = None,
    chave_nfe: Optional[str] = None,
    nro_coleta: Optional[int] = None,
    sigla_emp: Optional[str] = None,
    tipo_doc: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Consulta o rastreamento de UMA carga na WebAPI da SSW (POST /api/tracking).
    Informe exatamente um entre nro_nf, pedido, chave_nfe ou nro_coleta.

    Retorna o JSON bruto da resposta (sem conversão — ver docstring do módulo).
    Levanta SswTrackingError em qualquer falha (config ausente, rede, HTTP,
    corpo não-JSON).
    """
    if not is_configured(carrier):
        raise SswTrackingError(
            f"SSW não configurado para '{carrier}' — defina ao menos SSW_{carrier.upper()}_CNPJ no .env"
        )

    filtros = {"nro_nf": nro_nf, "pedido": pedido, "chave_nfe": chave_nfe, "nro_coleta": nro_coleta}
    informados = {k: v for k, v in filtros.items() if v not in (None, "")}
    if len(informados) != 1:
        raise SswTrackingError(
            "Informe exatamente um entre nro_nf, pedido, chave_nfe ou nro_coleta "
            f"(recebido: {list(informados)})"
        )

    payload: Dict[str, Any] = {"cnpj": _get(carrier, "CNPJ"), **informados}
    senha = _get(carrier, "SENHA")
    if senha:
        payload["senha"] = senha
    if sigla_emp:
        payload["sigla_emp"] = sigla_emp
    if tipo_doc:
        payload["tipo_doc"] = tipo_doc

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(TRACKING_URL, json=payload)
        except httpx.HTTPError as e:
            raise SswTrackingError(f"Falha de rede consultando SSW ({carrier}): {e}") from e

    if resp.status_code != 200:
        raise SswTrackingError(f"SSW respondeu {resp.status_code} para {carrier}: {resp.text[:300]}")

    try:
        return resp.json()
    except ValueError as e:
        raise SswTrackingError(f"Resposta da SSW não é JSON válido: {resp.text[:300]}") from e


def resposta_para_ocoren(
    resposta: Dict[str, Any],
    *,
    carrier: str,
    transportadora_cnpj: str,
    transportadora_nome: str,
    cnpj_emissor: str,
    serie: str,
    numero_nf: str,
) -> "OcorenParseResult":
    """
    Converte a resposta bruta de `consultar_rastreamento` para o mesmo formato
    usado pelo OCOREN por e-mail (OcorenParseResult), pronto para
    `app.tracking.import_ocoren` — reaproveita 100% do vínculo com a entrega,
    da trava de romaneio liberado e da tabela de códigos já existentes.

    cnpj_emissor/serie/numero_nf identificam a NF consultada (já conhecidos por
    quem chamou — a resposta da SSW não devolve esses campos de volta).
    """
    from app.ocoren_parser import OcorenParseResult, OcorrenciaEdi

    result = OcorenParseResult(
        arquivo=f"ssw-api://{carrier.lower()}/nf{numero_nf}",
        transportadora_cnpj=transportadora_cnpj,
        transportadora_nome=transportadora_nome,
        versao="SSW-API",
    )
    if not resposta.get("success"):
        return result  # documento não localizado / sem ocorrências ainda

    for i, ev in enumerate(resposta.get("tracking") or [], start=1):
        dt = ev.get("data_hora_efetiva") or ev.get("data_hora") or ""
        codigo_bruto = str(ev.get("codigo_ocorrencia") or "").strip()
        result.total_linhas += 1
        if not codigo_bruto or len(dt) < 10:
            result.linhas_rejeitadas.append((i, f"evento sem código/data válidos: {ev!r}"))
            continue
        result.ocorrencias.append(OcorrenciaEdi(
            cnpj_emissor=cnpj_emissor,
            serie=serie,
            numero_nf=numero_nf,
            codigo=f"SSW-{codigo_bruto}",
            data=dt[:10],
            hora=dt[11:16] if len(dt) >= 16 else None,
            codigo_observacao="",
            texto=ev.get("descricao") or ev.get("ocorrencia") or "",
            linha=i,
        ))
    return result
