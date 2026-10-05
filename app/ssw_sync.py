"""
Sincronizador automático de ocorrências via WebAPI de rastreamento da SSW
(Fitlog/TG) — mesmo padrão do carteiro de e-mail (app.email_ingest): lê
config do .env, roda sozinho em thread de fundo, e também pode ser disparado
manualmente. Alimenta o mesmo pipeline do OCOREN por e-mail (app.tracking.
import_ocoren) — ver app.ssw_client para o cliente da API e a conversão de
formato.

Só sincroniza entregas ainda EM ABERTO (sem data_entrega_real e não
devolvidas) — pra não ficar reconsultando pra sempre entregas já resolvidas.

Configuração no .env:
  SSW_SYNC_CARRIERS — transportadoras a sincronizar, separadas por vírgula
                       ("TG", "FITLOG" ou "TG,FITLOG"). Default: "TG".
                       Cada uma precisa de SSW_<CARRIER>_CNPJ e
                       SSW_<CARRIER>_SENHA configurados (app.ssw_client).
  SSW_AUTO           — "1" liga o sincronizador automático (default "1")
  SSW_INTERVALO_MIN  — intervalo entre sincronizações, em minutos (default 30)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app import ssw_client
from app.history import DB_PATH

logger = logging.getLogger("ssw_sync")

_ROOT = Path(__file__).parent.parent
_STATE_FILE = Path(os.getenv("SSW_SYNC_STATE", str(_ROOT / "tables" / "ssw_sync.json")))

# Chave curta (usada nas variáveis SSW_<CARRIER>_*) -> nome completo salvo em
# entregas.transportadora (mesmo nome usado no cadastro/carrier_mapping).
_CARRIER_NOME_COMPLETO = {
    "TG": "TG Transportes",
    "FITLOG": "Fitlog Transportes",
}


def _get(chave: str, default: str = "") -> str:
    return (os.getenv(chave, default) or "").strip()


def _carriers_configurados() -> List[str]:
    raw = _get("SSW_SYNC_CARRIERS", "TG")
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def configurado() -> bool:
    return any(ssw_client.is_configured(c) for c in _carriers_configurados())


def auto_ligado() -> bool:
    return _get("SSW_AUTO", "1").lower() not in ("0", "false", "nao", "não", "")


def intervalo_min() -> int:
    try:
        return max(5, int(_get("SSW_INTERVALO_MIN", "30")))
    except ValueError:
        return 30


# --------------------------------------------------------------------------- #
# Sincronização
# --------------------------------------------------------------------------- #
def _nfs_pendentes(nome_completo: str) -> List[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT DISTINCT n.cnpj_emissor, n.serie, n.numero_nf, e.transportadora_cnpj
            FROM entrega_nfs n
            JOIN entregas e ON e.chave_cte = n.chave_cte
            WHERE e.transportadora = ?
              AND e.data_entrega_real IS NULL
              AND (e.devolvida IS NULL OR e.devolvida = 0)
            """,
            (nome_completo,),
        ).fetchall()
    finally:
        conn.close()


async def _sincronizar_carrier(carrier: str) -> Dict:
    from app import tracking  # import tardio: evita ciclo no boot do FastAPI

    nome_completo = _CARRIER_NOME_COMPLETO.get(carrier)
    if not nome_completo:
        return {"carrier": carrier, "erro": "carrier não mapeado em ssw_sync._CARRIER_NOME_COMPLETO"}
    if not ssw_client.is_configured(carrier):
        return {"carrier": carrier, "erro": "credenciais não configuradas no .env"}

    pendentes = _nfs_pendentes(nome_completo)
    consultadas = erros = ocorrencias_novas = entregas_atualizadas = 0
    falhas = []

    for row in pendentes:
        numero_nf = row["numero_nf"]
        if not numero_nf or not str(numero_nf).isdigit():
            continue
        try:
            resposta = await ssw_client.consultar_rastreamento(carrier, nro_nf=int(numero_nf))
        except ssw_client.SswTrackingError as e:
            erros += 1
            if len(falhas) < 10:
                falhas.append({"numero_nf": numero_nf, "erro": str(e)})
            await asyncio.sleep(0.1)
            continue

        consultadas += 1
        parsed = ssw_client.resposta_para_ocoren(
            resposta, carrier=carrier, transportadora_cnpj=row["transportadora_cnpj"],
            transportadora_nome=nome_completo, cnpj_emissor=row["cnpj_emissor"],
            serie=row["serie"], numero_nf=numero_nf,
        )
        if parsed.ocorrencias:
            relatorio = tracking.import_ocoren(parsed)
            ocorrencias_novas += relatorio.get("ocorrencias_novas", 0)
            entregas_atualizadas += relatorio.get("entregas_atualizadas", 0)
        await asyncio.sleep(0.1)  # folga confortável sob o limite de 20 req/s da SSW

    return {
        "carrier": carrier, "transportadora": nome_completo,
        "nfs_pendentes": len(pendentes), "consultadas": consultadas, "erros": erros,
        "ocorrencias_novas": ocorrencias_novas, "entregas_atualizadas": entregas_atualizadas,
        "falhas": falhas,
    }


async def sincronizar() -> Dict:
    """Sincroniza todas as transportadoras configuradas em SSW_SYNC_CARRIERS.
    Transportadora sem credencial válida entra no resultado com 'erro', não
    interrompe as demais."""
    inicio = time.time()
    resultados = [await _sincronizar_carrier(c) for c in _carriers_configurados()]
    estado = {
        "ultima_sincronizacao": datetime.now().isoformat(timespec="seconds"),
        "segundos": round(time.time() - inicio, 1),
        "resultados": resultados,
    }
    _salvar_estado(estado)
    return estado


def _salvar_estado(estado: Dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning(f"Falha ao salvar estado do sincronizador SSW: {e}")


def _carregar_estado() -> Dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def status() -> Dict:
    estado = _carregar_estado()
    carriers = _carriers_configurados()
    return {
        "configurado": configurado(),
        "auto": auto_ligado(),
        "intervalo_min": intervalo_min(),
        "carriers": carriers,
        "carriers_prontos": [c for c in carriers if ssw_client.is_configured(c)],
        "ultima_sincronizacao": estado.get("ultima_sincronizacao"),
        "ultimo_resultado": estado.get("resultados"),
    }


# --------------------------------------------------------------------------- #
# Auto (poller em segundo plano)
# --------------------------------------------------------------------------- #
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop() -> None:
    _stop.wait(30)
    while not _stop.is_set():
        try:
            if configurado() and auto_ligado():
                res = asyncio.run(sincronizar())
                total_novas = sum(r.get("ocorrencias_novas", 0) for r in res.get("resultados", []))
                if total_novas:
                    logger.info(f"SSW sync: {total_novas} ocorrência(s) nova(s).")
        except Exception as e:
            logger.error(f"SSW sync auto falhou: {e}")
        _stop.wait(intervalo_min() * 60)


def iniciar_auto() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    if not configurado():
        logger.info("SSW sync: nenhuma transportadora configurada — auto desligado.")
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="ssw-sync", daemon=True)
    _thread.start()
    logger.info(f"SSW sync: auto ligado (a cada {intervalo_min()} min) para {_carriers_configurados()}.")


def parar_auto() -> None:
    _stop.set()
