# -*- coding: utf-8 -*-
"""Teste de reincidência por cidade/rota (#7): praças-problema recorrentes.
Monkeypatcha _universo para não depender do DB."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import ocorrencias  # noqa: E402


def _oc(cat):
    return {"categoria": cat}


def test_reincidencia_por_cidade():
    universo = [
        # Feira de Santana/BA · Jamef: 3 entregas, 2 com tentativa frustrada
        {"destino": "Feira de Santana/BA", "uf": "BA", "transportadora": "Jamef", "valor": 1000,
         "ocorrencias": [_oc("TENTATIVA_FRUSTRADA")]},
        {"destino": "Feira de Santana/BA", "uf": "BA", "transportadora": "Jamef", "valor": 2000,
         "ocorrencias": [_oc("TENTATIVA_FRUSTRADA"), _oc("TENTATIVA_FRUSTRADA")]},
        {"destino": "Feira de Santana/BA", "uf": "BA", "transportadora": "Jamef", "valor": 500,
         "ocorrencias": []},
        # São Paulo/SP · TG: só 1 afetada → abaixo do mínimo, não entra
        {"destino": "São Paulo/SP", "uf": "SP", "transportadora": "TG", "valor": 300,
         "ocorrencias": [_oc("SINISTRO")]},
        # ocorrência neutra não conta como praça-problema
        {"destino": "Curitiba/PR", "uf": "PR", "transportadora": "Fitlog", "valor": 100,
         "ocorrencias": [_oc("SAIDA_ENTREGA")]},
    ]
    orig = ocorrencias._universo
    ocorrencias._universo = lambda mes=None: list(universo)
    try:
        r = ocorrencias.reincidencia_por_cidade(min_afetadas=2)
    finally:
        ocorrencias._universo = orig

    assert len(r) == 1, r
    top = r[0]
    assert top["destino"] == "Feira de Santana/BA" and top["transportadora"] == "Jamef"
    assert top["entregas_total"] == 3 and top["entregas_afetadas"] == 2
    assert top["ocorrencias"] == 3            # 1 + 2 tentativas
    assert top["taxa_pct"] == 66.7
    assert top["valor_risco"] == 3000.0
    assert "frustrada" in (top["motivo_top"] or "").lower() or "TENTATIVA" in (top["motivo_top"] or "")
    print("OK reincidência por cidade (praça-problema recorrente, filtro por mínimo)")


if __name__ == "__main__":
    test_reincidencia_por_cidade()
    print("\nTESTE DE REINCIDÊNCIA (#7) PASSOU.")
