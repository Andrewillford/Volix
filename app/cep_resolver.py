"""
Resolve CEP → {city, uf} usando a API pública ViaCEP.
Usado para transportadoras que não fornecem faixas de CEP na tabela de prazos
(Mobile, TG_Transportes, Modular) — nesses casos, fazemos match por nome de cidade.

Cache em memória para evitar chamadas repetidas na mesma sessão.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Dict, Tuple

import httpx

logger = logging.getLogger(__name__)

_cache: Dict[str, Optional[Tuple[str, str]]] = {}  # cep → (cidade_upper, uf_upper)


async def _via_viacep(client: httpx.AsyncClient, cep: str) -> Optional[Tuple[str, str]]:
    resp = await client.get(f"https://viacep.com.br/ws/{cep}/json/")
    if resp.status_code != 200:
        raise httpx.HTTPError(f"status {resp.status_code}")
    data = resp.json()
    if "erro" in data:  # CEP inexistente: resposta definitiva, não adianta o fallback
        return None
    city = (data.get("localidade") or "").upper().strip()
    uf = (data.get("uf") or "").upper().strip()
    return (city, uf) if city and uf else None


async def _via_brasilapi(client: httpx.AsyncClient, cep: str) -> Optional[Tuple[str, str]]:
    resp = await client.get(f"https://brasilapi.com.br/api/cep/v1/{cep}")
    if resp.status_code == 404:  # CEP inexistente
        return None
    if resp.status_code != 200:
        raise httpx.HTTPError(f"status {resp.status_code}")
    data = resp.json()
    city = (data.get("city") or "").upper().strip()
    uf = (data.get("state") or "").upper().strip()
    return (city, uf) if city and uf else None


# Provedores em ordem de preferência. Se um falhar por rede/erro (não por CEP
# inexistente), tenta o próximo — evita que a auditoria/cotação fique sem prazo
# quando a ViaCEP está fora do ar.
_PROVIDERS = (("ViaCEP", _via_viacep), ("BrasilAPI", _via_brasilapi))


async def resolve_cep(cep: str) -> Optional[Tuple[str, str]]:
    """
    Retorna (CIDADE_EM_MAIUSCULAS, UF) para o CEP dado.
    Retorna None se o CEP não for encontrado (por nenhum provedor).
    """
    cep_clean = re.sub(r"\D", "", cep).zfill(8)

    if cep_clean in _cache:
        return _cache[cep_clean]

    ultimo_erro: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=5.0) as client:
        for nome, provider in _PROVIDERS:
            try:
                result = await provider(client, cep_clean)
            except Exception as e:
                ultimo_erro = e
                logger.warning(f"{nome} falhou para CEP {cep_clean}: {e} — tentando próximo provedor")
                continue
            # Sucesso definitivo (achou) ou CEP inexistente confirmado (None).
            _cache[cep_clean] = result
            if result:
                logger.debug(f"CEP {cep_clean} → {result[0]}/{result[1]} via {nome}")
            else:
                logger.warning(f"CEP não encontrado em {nome}: {cep_clean}")
            return result

    # Todos os provedores falharam por rede/erro — NÃO cacheia (foi indisponibilidade,
    # não CEP inexistente); a próxima tentativa pode ter sucesso.
    logger.error(f"Nenhum provedor de CEP respondeu para {cep_clean}: {ultimo_erro}")
    return None
