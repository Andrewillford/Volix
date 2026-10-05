"""
Mapeamento CNPJ do emitente do CTe -> nome da aba da transportadora nas tabelas.

Persistido em JSON (tables/transportadoras_cnpj.json) para poder editar sem
redeploy. Um CNPJ pode ser marcado como SPOT_SENTINEL quando é um emitente
conhecido mas sem tabela contratada (frete avulso) — nesse caso o auditor não
pergunta de novo, só classifica como SPOT direto.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, Optional

SPOT_SENTINEL = "__SPOT__"

MAPPING_FILE = os.getenv(
    "CARRIER_MAPPING_FILE",
    str(Path(__file__).parent.parent / "tables" / "transportadoras_cnpj.json"),
)


def _clean_cnpj(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def load_mapping() -> Dict[str, str]:
    p = Path(MAPPING_FILE)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_mapping(mapping: Dict[str, str]) -> None:
    p = Path(MAPPING_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def get_carrier(cnpj: str, mapping: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Retorna o nome da aba da transportadora, ou None se não mapeado/SPOT."""
    mapping = mapping if mapping is not None else load_mapping()
    val = mapping.get(_clean_cnpj(cnpj))
    if not val or val == SPOT_SENTINEL:
        return None
    return val


def is_known_spot(cnpj: str, mapping: Optional[Dict[str, str]] = None) -> bool:
    mapping = mapping if mapping is not None else load_mapping()
    return mapping.get(_clean_cnpj(cnpj)) == SPOT_SENTINEL


def set_mapping(cnpj: str, carrier_name: Optional[str]) -> Dict[str, str]:
    """carrier_name=None marca como SPOT conhecido (sem tabela contratada)."""
    mapping = load_mapping()
    mapping[_clean_cnpj(cnpj)] = carrier_name if carrier_name else SPOT_SENTINEL
    save_mapping(mapping)
    return mapping
