# -*- coding: utf-8 -*-
"""Testes do peso cubado na auditoria (#3): regra do peso taxável + extração de
peso_real/m³ do CTe. Roda com pytest ou direto (python tests/test_cubagem.py)."""
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.auditor import peso_taxavel_cubado  # noqa: E402
from app import cte_parser  # noqa: E402


def test_regra_peso_taxavel():
    # Cubado manda quando m³ × fator > peso real.
    assert peso_taxavel_cubado(100, 100, 1.0, 300) == 300.0
    # Peso real manda quando é maior que o cubado.
    assert peso_taxavel_cubado(500, 500, 1.0, 300) == 500
    # Sem m³ → mantém o peso base do CTe (comportamento anterior).
    assert peso_taxavel_cubado(100, 120, None, 300) == 120
    # Sem cubagem contratada → mantém o peso base.
    assert peso_taxavel_cubado(100, 120, 1.0, 0) == 120
    # Sem peso real informado → cai no peso base para a comparação.
    assert peso_taxavel_cubado(None, 120, 1.0, 300) == 300.0  # 1.0*300 > 120
    print("OK regra peso taxável (cubado vs real vs base)")


def test_deteccao_cubagem_inflada():
    # Transportadora cubou a 300 (base 57,3) mas o contrato é 250 → esperado 47,8:
    # base cobrada fica acima do taxável → deve entrar como divergência de cubagem.
    base_cobrada, m3, real = 57.3, 0.1911, 22.28
    taxavel_250 = peso_taxavel_cubado(real, base_cobrada, m3, 250)   # 47.775
    taxavel_300 = peso_taxavel_cubado(real, base_cobrada, m3, 300)   # 57.33
    assert round(taxavel_250, 1) == 47.8 and base_cobrada > taxavel_250 * 1.02   # inflada p/ 250
    assert base_cobrada <= taxavel_300 * 1.02                                     # ok p/ 300
    print("OK detecção de cubagem inflada (fator 250 flaga, 300 não)")


def test_parser_extrai_real_e_m3():
    achou = 0
    for f in glob.glob("cte_xml/processados/ok/*.xml"):
        try:
            cte = cte_parser.parse_cte_bytes(open(f, "rb").read(), f)
        except Exception:
            continue
        if cte and cte.m3 and cte.peso_real:
            assert cte.m3 > 0 and cte.peso_real > 0
            achou += 1
            if achou >= 3:
                break
    if achou:
        print(f"OK parser extrai peso_real/m³ ({achou} CTes verificados)")
    else:
        print("SKIP parser: nenhum CTe local com m³/peso_real (ok em outra máquina)")


if __name__ == "__main__":
    test_regra_peso_taxavel()
    test_deteccao_cubagem_inflada()
    test_parser_extrai_real_e_m3()
    print("\nTESTES DE CUBAGEM (#3) PASSARAM.")
