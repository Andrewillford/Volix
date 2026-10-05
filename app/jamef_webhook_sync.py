"""
Sincronizador do webhook de ocorrências da Jamef.

A Jamef só oferece webhook (push) pra ocorrências, não uma API de consulta
como a SSW. Como o sistema roda 100% local, criamos um recebedor ISOLADO
(Cloudflare Worker, sem nenhuma ligação com o sistema principal — só recebe
o POST da Jamef e guarda numa fila) e este módulo BUSCA (pull) os itens
acumulados nesse recebedor, periodicamente, como se fosse mais uma caixa
postal — mesmo padrão do app.email_ingest e do app.ssw_sync.

O payload da Jamef já usa o código PROCEDA nativo (codigoOcorrencia), então
NÃO precisa de nenhuma tradução/catálogo como a SSW exigiu — o código cai
direto em tables/ocorrencias_codigos.json, a mesma tabela do OCOREN por
e-mail. Ver o schema completo do payload no docstring de `_payload_para_ocoren`.

Fonte do recebedor (Worker isolado, sem acesso a nada deste sistema):
  jamef-webhook-receiver/  (pasta irmã, fora deste repo)

Configuração no .env:
  JAMEF_WEBHOOK_URL         — URL base do recebedor, ex.:
                               https://jamef-webhook-receiver.SEUSUBDOMINIO.workers.dev
  JAMEF_WEBHOOK_POLL_TOKEN  — token que o sistema local usa pra buscar/confirmar
                               (é o POLL_TOKEN configurado no Worker; o
                               INBOUND_TOKEN é só pra Jamef, não fica aqui)
  JAMEF_AUTO                — "1" liga o sincronizador automático (default "1")
  JAMEF_INTERVALO_MIN       — intervalo entre sincronizações, em minutos (default 15)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("jamef_webhook_sync")

_ROOT = Path(__file__).parent.parent
_STATE_FILE = Path(os.getenv("JAMEF_WEBHOOK_STATE", str(_ROOT / "tables" / "jamef_webhook_sync.json")))

# CNPJ da Jamef usado no sistema (mesmo já cadastrado em carrier_mapping) —
# só pra preencher o campo informativo transportadora_cnpj do OCOREN; o
# payload do webhook não confirma esse dado de forma confiável.
JAMEF_CNPJ = "20147617002608"
JAMEF_NOME = "Jamef Encomendas Urgentes"


def _load_env() -> None:
    env_file = _ROOT / ".env"
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
            if chave.startswith("JAMEF_") and chave not in os.environ:
                os.environ[chave] = valor
    except OSError as e:
        logger.warning(f"Falha ao ler .env: {e}")


_load_env()


def _get(campo: str, default: str = "") -> str:
    return (os.getenv(f"JAMEF_{campo}", default) or "").strip()


def configurado() -> bool:
    return bool(_get("WEBHOOK_URL") and _get("WEBHOOK_POLL_TOKEN"))


def auto_ligado() -> bool:
    return _get("AUTO", "1").lower() not in ("0", "false", "nao", "não", "")


def intervalo_min() -> int:
    try:
        return max(5, int(_get("INTERVALO_MIN", "15")))
    except ValueError:
        return 15


# --------------------------------------------------------------------------- #
# Conversão do payload da Jamef para o formato interno (OcorenParseResult)
# --------------------------------------------------------------------------- #
def _parse_data_hora(raw: str) -> tuple[Optional[str], Optional[str]]:
    """'11-08-2025T08:42:00' -> ('2025-08-11', '08:42'). None se malformado."""
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})T(\d{2}):(\d{2})", raw or "")
    if not m:
        return None, None
    dd, mm, yyyy, hh, mi = m.groups()
    return f"{yyyy}-{mm}-{dd}", f"{hh}:{mi}"


# Mapa texto->código sintético para eventos SEM codigoOcorrencia (movimentações
# internas que a Jamef manda só como texto: em rota, transferências, saída de
# filial). Mesmo padrão dos códigos SSW-XX: entra em ocorrencias_codigos.json.
_STATUS_SINTETICOS = [
    ("EM ROTA DE ENTREGA", "JAMEF-ROTA"),
    ("ROTA DE ENTREGA", "JAMEF-ROTA"),
    ("SAIDA PARA ENTREGA", "JAMEF-ROTA"),
    ("SAIU PARA ENTREGA", "JAMEF-ROTA"),
    ("CHEGADA TRANSFERENCIA", "JAMEF-CHEGADA-TRANSF"),
    ("CHEGADA DE TRANSFERENCIA", "JAMEF-CHEGADA-TRANSF"),
    ("CHEGADA NA TRANSFERENCIA", "JAMEF-CHEGADA-TRANSF"),
    ("SAIDA TRANSFERENCIA", "JAMEF-SAIDA-TRANSF"),
    ("SAIDA DE TRANSFERENCIA", "JAMEF-SAIDA-TRANSF"),
    ("SAIDA FILIAL", "JAMEF-SAIDA-FILIAL"),
    ("SAIDA DA FILIAL", "JAMEF-SAIDA-FILIAL"),
]


def _codigo_jamef(codigo_raw: str) -> str:
    """Namespace dos códigos da Jamef: 'JAMEF-NN'. A Jamef reusa os números PROCEDA
    com significado PRÓPRIO (ex.: 098 = 'saída de transferência', não 'chegada na
    cidade'; 010 = 'local fechado', não 'frete pago'), então NÃO podem cair na
    tabela PROCEDA global — senão colidem com o OCOREN dos outros. Normaliza o
    número (sem zeros à esquerda, mín. 2 dígitos): '098'->'JAMEF-98', '001'->'JAMEF-01'."""
    c = str(codigo_raw or "").strip()
    if not c:
        return ""
    num = c.lstrip("0") or "0"
    if num.isdigit() and len(num) < 2:
        num = num.zfill(2)
    return f"JAMEF-{num}"


def _sintetizar_codigo(status: str) -> str:
    """Deriva um código a partir do texto livre `status` quando o evento vem sem
    codigoOcorrencia — assim não se perde a movimentação (ex.: 'EM ROTA DE ENTREGA').
    Casa por palavra-chave (acento/caixa ignorados); texto desconhecido vira um
    slug estável JAMEF-<...> que você mapeia depois em ocorrencias_codigos.json."""
    import unicodedata
    s = "".join(
        c for c in unicodedata.normalize("NFKD", status or "") if not unicodedata.combining(c)
    ).upper().strip()
    if not s:
        return ""
    for chave, cod in _STATUS_SINTETICOS:
        if chave in s:
            return cod
    slug = re.sub(r"[^A-Z0-9]+", "-", s).strip("-")[:24]
    return f"JAMEF-{slug}" if slug else ""


def _payload_para_ocoren(payload: Dict[str, Any], arquivo: str):
    """
    Schema do payload (confirmado na documentação da Jamef, 2026-09-03):
    {
      "rastreamento": [
        {
          "tipo": "CTE",
          "remetente": {"cnpj": "...", "nome": "...", "cidade": "...", "uf": "..."},
          "destinatario": {"cnpj": "...", ...},
          "pagadorFrete": {...},
          "conhecimento": {"numero": "...", "serie": "...", "chave": "..."},
          "notaFiscal": {"numero": "...", "serie": "...", "chave": "...",
                         "quantidadeVolume": N, "pedido": "..."},
          "frete": {"valorFrete": N, "previsaoEntrega": "DD-MM-AAAA"},
          "eventosRastreio": [
            {"data": "DD-MM-AAAATHH:MM:SS", "status": "texto livre",
             "codigoOcorrencia": "01" (código PROCEDA nativo),
             "localOrigem": {"cidade": "...", "uf": "..."},
             "localDestino": {"cidade": "...", "uf": "..."}}
          ]
        }
      ]
    }

    codigoOcorrencia já é o código PROCEDA padrão — não precisa de tradução,
    cai direto em tables/ocorrencias_codigos.json (mesma tabela do OCOREN por
    e-mail/arquivo).
    """
    from app.ocoren_parser import OcorenParseResult, OcorrenciaEdi

    result = OcorenParseResult(
        arquivo=arquivo, transportadora_cnpj=JAMEF_CNPJ, transportadora_nome=JAMEF_NOME,
        versao="JAMEF-WEBHOOK",
    )
    linha = 0
    for item in payload.get("rastreamento") or []:
        nf = item.get("notaFiscal") or {}
        remetente = item.get("remetente") or {}
        cnpj_emissor = re.sub(r"\D", "", str(remetente.get("cnpj") or ""))
        serie = str(nf.get("serie") or "").strip()
        numero_nf = str(nf.get("numero") or "").strip().lstrip("0") or "0"

        for ev in item.get("eventosRastreio") or []:
            linha += 1
            data_iso, hora = _parse_data_hora(ev.get("data") or "")
            status_txt = ev.get("status") or ""
            codigo_raw = str(ev.get("codigoOcorrencia") or "").strip()
            # Com código: namespace 'JAMEF-NN' (a Jamef reusa os números PROCEDA com
            # sentido próprio). Sem código: sintetiza pelo texto (em rota, etc.).
            codigo = _codigo_jamef(codigo_raw) if codigo_raw else _sintetizar_codigo(status_txt)
            # Código 99 = 'outros não especificados' (catch-all): o texto é que
            # carrega o sentido (ex.: 'MERCADORIA EM ROTA DE ENTREGA') — deriva dele.
            if codigo in ("JAMEF-99", ""):
                sint = _sintetizar_codigo(status_txt)
                if sint:
                    codigo = sint
            result.total_linhas += 1
            if not codigo or not data_iso or not cnpj_emissor:
                result.linhas_rejeitadas.append((linha, f"evento sem dados válidos: {ev!r}"))
                continue
            result.ocorrencias.append(OcorrenciaEdi(
                cnpj_emissor=cnpj_emissor,
                serie=serie,
                numero_nf=numero_nf,
                codigo=codigo,
                data=data_iso,
                hora=hora,
                codigo_observacao="",
                texto=status_txt,
                linha=linha,
            ))
    return result


# --------------------------------------------------------------------------- #
# Sincronização (busca no recebedor -> importa -> confirma)
# --------------------------------------------------------------------------- #
async def sincronizar() -> Dict:
    if not configurado():
        return {"erro": "JAMEF_WEBHOOK_URL/JAMEF_WEBHOOK_POLL_TOKEN não configurados no .env"}

    from app import tracking
    tracking.init_db()

    base = _get("WEBHOOK_URL").rstrip("/")
    token = _get("WEBHOOK_POLL_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    inicio = time.time()

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{base}/pending", headers=headers)
        resp.raise_for_status()
        data = resp.json()

    itens = data.get("itens") or []
    ocorrencias_novas = entregas_atualizadas = erros_parse = 0
    ack_keys: List[str] = []

    for item in itens:
        try:
            payload = json.loads(item["payload"])
        except (ValueError, KeyError) as e:
            erros_parse += 1
            logger.warning(f"Payload inválido em {item.get('key')}: {e}")
            continue

        parsed = _payload_para_ocoren(payload, arquivo=f"jamef-webhook://{item['key']}")
        if parsed.ocorrencias:
            relatorio = tracking.import_ocoren(parsed)
            ocorrencias_novas += relatorio.get("ocorrencias_novas", 0)
            entregas_atualizadas += relatorio.get("entregas_atualizadas", 0)
        ack_keys.append(item["key"])  # confirma mesmo se não tinha ocorrência válida (não reprocessa lixo)

    if ack_keys:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(f"{base}/ack", json={"keys": ack_keys}, headers=headers)

    estado = {
        "ultima_sincronizacao": datetime.now().isoformat(timespec="seconds"),
        "segundos": round(time.time() - inicio, 1),
        "itens_recebidos": len(itens),
        "confirmados": len(ack_keys),
        "ocorrencias_novas": ocorrencias_novas,
        "entregas_atualizadas": entregas_atualizadas,
        "erros_parse": erros_parse,
        "mais_pendentes": bool(data.get("mais")),
    }
    _salvar_estado(estado)
    return estado


def _salvar_estado(estado: Dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning(f"Falha ao salvar estado: {e}")


def _carregar_estado() -> Dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def status() -> Dict:
    estado = _carregar_estado()
    return {
        "configurado": configurado(),
        "auto": auto_ligado(),
        "intervalo_min": intervalo_min(),
        "ultima_sincronizacao": estado.get("ultima_sincronizacao"),
        "ultimo_resultado": estado,
    }


# --------------------------------------------------------------------------- #
# Auto (poller em segundo plano)
# --------------------------------------------------------------------------- #
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop() -> None:
    import asyncio
    _stop.wait(30)
    while not _stop.is_set():
        try:
            if configurado() and auto_ligado():
                res = asyncio.run(sincronizar())
                if res.get("ocorrencias_novas"):
                    logger.info(f"Jamef webhook sync: {res['ocorrencias_novas']} ocorrência(s) nova(s).")
        except Exception as e:
            logger.error(f"Jamef webhook sync auto falhou: {e}")
        _stop.wait(intervalo_min() * 60)


def iniciar_auto() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    if not configurado():
        logger.info("Jamef webhook sync: não configurado — auto desligado.")
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="jamef-webhook-sync", daemon=True)
    _thread.start()
    logger.info(f"Jamef webhook sync: auto ligado (a cada {intervalo_min()} min).")


def parar_auto() -> None:
    _stop.set()
