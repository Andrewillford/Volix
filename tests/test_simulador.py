# -*- coding: utf-8 -*-
"""Teste do simulador de reajuste (#4): re-cota CTes históricos contra a tabela
reajustada e mede o impacto. Roda com pytest ou direto."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.cadastro import _precos_data_from_rows, _prazos_df_from_rows  # noqa: E402
from app import simulador  # noqa: E402


def _tabela():
    meta = {}  # todos os encargos zerados → só frete-peso (e ICMS proporcional)
    precos = [{"uf": "SP", "regiao": "", "faixas": {"f20": 100.0, "f50": 150.0},
               "advalorem_pct": 0, "frete_val_pct": 0, "sec_cat": 0, "pedagio_col": 0, "despacho": None}]
    precos_data = _precos_data_from_rows("Teste", "Vitória-ES", meta, precos)
    prazos_df = _prazos_df_from_rows([{"uf": "SP", "cidade": "SAO PAULO", "regiao": "",
                                       "cep_inicial": "", "cep_final": "", "prazo_dias": 3}])
    return precos_data, prazos_df


def test_simulacao_reajuste():
    precos_data, prazos_df = _tabela()
    historico = [
        {"peso_kg": 18, "destino": "São Paulo/SP", "valor_mercadoria": 0},
        {"peso_kg": 40, "destino": "São Paulo/SP", "valor_mercadoria": 0},
        {"peso_kg": 20, "destino": "São Paulo/SP", "valor_mercadoria": 0},
    ]
    r = simulador.simular("Teste", precos_data, prazos_df, historico, pct=10)
    assert r["ctes"] == 3, r
    assert r["custo_atual"] > 0
    # Reajuste linear de 10% em componentes R$ → impacto total de ~10%.
    assert abs(r["delta_pct"] - 10.0) < 0.01, r["delta_pct"]
    assert abs(r["custo_simulado"] - r["custo_atual"] * 1.10) < 0.02
    # Quebra por UF presente e somando o delta.
    assert r["por_uf"] and r["por_uf"][0]["uf"] == "SP"
    assert abs(r["por_uf"][0]["delta"] - r["delta"]) < 0.01
    print(f"OK simulador: {r['ctes']} CTes, atual R${r['custo_atual']:.2f} -> "
          f"simulado R${r['custo_simulado']:.2f} (+{r['delta_pct']}%)")


def test_reajuste_zero_nao_muda():
    precos_data, prazos_df = _tabela()
    historico = [{"peso_kg": 20, "destino": "São Paulo/SP", "valor_mercadoria": 0}]
    r = simulador.simular("Teste", precos_data, prazos_df, historico, pct=0)
    assert r["delta"] == 0.0 and r["custo_atual"] == r["custo_simulado"]
    print("OK simulador: reajuste 0% não altera custo")


if __name__ == "__main__":
    test_simulacao_reajuste()
    test_reajuste_zero_nao_muda()
    print("\nTESTES DO SIMULADOR (#4) PASSARAM.")
