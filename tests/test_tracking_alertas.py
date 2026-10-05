# -*- coding: utf-8 -*-
"""Testes dos alertas da Torre (#5): entrega em risco + cobertura de rastreio.
Monkeypatcha query_entregas para não depender do schema/DB da torre."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import tracking  # noqa: E402


def _iso(delta_dias):
    return (date.today() + timedelta(days=delta_dias)).isoformat()


def test_entregas_em_risco():
    fake = [
        # vence em 1 dia, em trânsito, sem saída para entrega → RISCO
        {"chave_cte": "A", "status": "EM_TRANSITO", "data_prevista": _iso(1), "rota": False,
         "transportadora": "Jamef", "destino": "SP", "dias_atraso": None},
        # já atrasada 3 dias, sem saída → RISCO (mais urgente)
        {"chave_cte": "B", "status": "ATRASADO", "data_prevista": _iso(-3), "rota": False,
         "transportadora": "TG", "destino": "BA", "dias_atraso": 3},
        # vence em 1 dia MAS já saiu para entrega (rota) → NÃO é alvo
        {"chave_cte": "C", "status": "EM_TRANSITO", "data_prevista": _iso(1), "rota": True,
         "transportadora": "Jamef", "destino": "MG"},
        # vence só daqui a 10 dias → fora da janela
        {"chave_cte": "D", "status": "EM_TRANSITO", "data_prevista": _iso(10), "rota": False,
         "transportadora": "Fitlog", "destino": "RS"},
        # já entregue → fora
        {"chave_cte": "E", "status": "ENTREGUE_NO_PRAZO", "data_prevista": _iso(-1), "rota": False,
         "transportadora": "Jamef", "destino": "SP"},
    ]
    orig = tracking.query_entregas
    tracking.query_entregas = lambda **kw: list(fake)
    try:
        risco = tracking.entregas_em_risco(dias=2)
    finally:
        tracking.query_entregas = orig
    chaves = [r["chave_cte"] for r in risco]
    assert chaves == ["B", "A"], chaves          # B (atrasada) antes de A (vence amanhã)
    assert risco[0]["dias_para_vencer"] == -3
    print("OK entregas em risco (janela, exclui rota/entregue, ordena por urgência)")


def test_cobertura_rastreio():
    fake = [
        {"transportadora": "Jamef", "total_ocorrencias": 2},
        {"transportadora": "Jamef", "total_ocorrencias": 1},
        {"transportadora": "Jamef", "total_ocorrencias": 0},
        {"transportadora": "SemEDI", "total_ocorrencias": 0},
        {"transportadora": "SemEDI", "total_ocorrencias": 0},
    ]
    orig = tracking.query_entregas
    tracking.query_entregas = lambda **kw: list(fake)
    try:
        cob = tracking.cobertura_rastreio()
    finally:
        tracking.query_entregas = orig
    # Pior cobertura primeiro: SemEDI 0% antes de Jamef 66.7%.
    assert cob[0]["transportadora"] == "SemEDI" and cob[0]["cobertura_pct"] == 0.0
    jam = [c for c in cob if c["transportadora"] == "Jamef"][0]
    assert jam["com_evento"] == 2 and jam["total"] == 3 and jam["cobertura_pct"] == 66.7
    print("OK cobertura de rastreio (por transportadora, pior primeiro)")


if __name__ == "__main__":
    test_entregas_em_risco()
    test_cobertura_rastreio()
    print("\nTESTES DE ALERTAS DA TORRE (#5) PASSARAM.")
