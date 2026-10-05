"""
Feriados brasileiros para o cálculo de dias úteis (prazo de entrega, data
prevista, OTD). Sem dependência externa e sem rede: os feriados móveis são
derivados da Páscoa (algoritmo de Meeus/Butcher).

Cobre os feriados NACIONAIS (fixos + móveis). Feriados estaduais/municipais
variam por origem/destino e não são universais, então ficam numa lista
editável opcional `tables/feriados.json` (mesmo espírito de
`ocorrencias_codigos.json`):

    {
      "incluir_carnaval": true,        // 2ª e 3ª de Carnaval (ponto facultativo, mas para na prática)
      "incluir_corpus_christi": true,  // Corpus Christi (idem)
      "extras": ["2026-04-13"],        // acrescenta (ex.: feriado municipal da origem)
      "remover": ["2026-06-04"]        // remove um que a empresa não observa
    }

Se o arquivo não existir, valem os defaults (todos os nacionais + Carnaval +
Corpus Christi, que na logística efetivamente interrompem a entrega).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

FERIADOS_FILE = os.getenv(
    "FERIADOS_FILE",
    str(Path(__file__).parent.parent / "tables" / "feriados.json"),
)

_WEEKEND = {5, 6}
_cache_ano: Dict[int, Set[date]] = {}
_config_cache: Optional[Dict] = None


def _pascoa(ano: int) -> date:
    """Domingo de Páscoa (algoritmo Anonymous Gregorian / Meeus-Butcher)."""
    a = ano % 19
    b = ano // 100
    c = ano % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(ano, mes, dia)


def _load_config(force: bool = False) -> Dict:
    global _config_cache
    if _config_cache is not None and not force:
        return _config_cache
    cfg = {"incluir_carnaval": True, "incluir_corpus_christi": True, "extras": [], "remover": []}
    try:
        raw = json.loads(Path(FERIADOS_FILE).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update({k: raw[k] for k in cfg if k in raw})
    except FileNotFoundError:
        pass  # sem arquivo → defaults
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Falha lendo {FERIADOS_FILE} (usando defaults): {e}")
    _config_cache = cfg
    return cfg


def _parse_datas(valores) -> Set[date]:
    out: Set[date] = set()
    for v in (valores or []):
        try:
            out.add(date.fromisoformat(str(v)[:10]))
        except (ValueError, TypeError):
            logger.warning(f"Feriado com data inválida ignorado: {v!r}")
    return out


def feriados_do_ano(ano: int, force: bool = False) -> Set[date]:
    """Conjunto de feriados (nacionais + configurados) do ano, com cache."""
    if not force and ano in _cache_ano:
        return _cache_ano[ano]

    cfg = _load_config(force=force)
    dias: Set[date] = {
        date(ano, 1, 1),    # Confraternização Universal
        date(ano, 4, 21),   # Tiradentes
        date(ano, 5, 1),    # Dia do Trabalho
        date(ano, 9, 7),    # Independência
        date(ano, 10, 12),  # N. Sra. Aparecida
        date(ano, 11, 2),   # Finados
        date(ano, 11, 15),  # Proclamação da República
        date(ano, 12, 25),  # Natal
    }
    if ano >= 2024:
        dias.add(date(ano, 11, 20))  # Consciência Negra (Lei 14.759/2023)

    pascoa = _pascoa(ano)
    dias.add(pascoa - timedelta(days=2))  # Sexta-feira Santa (nacional)
    if cfg.get("incluir_carnaval", True):
        dias.add(pascoa - timedelta(days=48))  # Segunda de Carnaval
        dias.add(pascoa - timedelta(days=47))  # Terça de Carnaval
    if cfg.get("incluir_corpus_christi", True):
        dias.add(pascoa + timedelta(days=60))  # Corpus Christi

    dias |= _parse_datas(cfg.get("extras"))
    dias -= _parse_datas(cfg.get("remover"))

    _cache_ano[ano] = dias
    return dias


def eh_feriado(d: date) -> bool:
    return d in feriados_do_ano(d.year)


def eh_dia_util(d: date) -> bool:
    return d.weekday() not in _WEEKEND and not eh_feriado(d)


def somar_dias_uteis(inicio: date, dias: int) -> date:
    """Avança `dias` dias úteis a partir de `inicio`, pulando fins de semana
    E feriados. Mantém a semântica anterior (não conta o dia de início)."""
    atual, add = inicio, 0
    while add < dias:
        atual += timedelta(days=1)
        if eh_dia_util(atual):
            add += 1
    return atual


def reload_feriados() -> int:
    """Limpa os caches para reler o JSON. Retorna quantos feriados o ano atual tem."""
    _cache_ano.clear()
    _load_config(force=True)
    return len(feriados_do_ano(date.today().year, force=True))
