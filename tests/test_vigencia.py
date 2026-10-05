# -*- coding: utf-8 -*-
"""Testes de vigência de tabela (#2): auditar contra a tabela válida na emissão.
DB temporário por env; roda com pytest ou direto."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["CADASTRO_DB"] = os.path.join(tempfile.mkdtemp(prefix="loghub_vig_"), "cadastro.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import cadastro  # noqa: E402


def _preco(uf, faixa20):
    return {"uf": uf, "regiao": "", "descricao": "", "faixas": {"f20": faixa20},
            "advalorem_pct": 0, "frete_val_pct": 0, "sec_cat": 0, "pedagio_col": 0, "despacho": None}


def test_vigencia_tabela():
    cadastro.init_db()
    t = cadastro.create_transportadora({"nome": "Teste Vig", "origem": "Vitória-ES"})
    tid = t["id"]
    # Tabela "antiga": faixa 20kg = 100.
    cadastro.save_precos(tid, [_preco("SP", 100.0)])
    cadastro.save_prazos(tid, [{"uf": "SP", "cidade": "SAO PAULO", "regiao": "",
                                "cep_inicial": "", "cep_final": "", "prazo_dias": 3}])

    # Sem vigência configurada → tabela atual se aplica a qualquer data.
    assert cadastro.tabela_vigente_em(tid, "2020-01-01") is None

    # Reajuste a partir de 2026-09-01: arquiva a tabela antiga (faixa=100).
    cadastro.arquivar_e_reajustar(tid, "2026-09-01", "Reajuste anual")
    # Agora edita a tabela viva para os valores novos: faixa 20kg = 130.
    cadastro.save_precos(tid, [_preco("SP", 130.0)])

    # CTe emitido ANTES do reajuste → tabela antiga (100).
    hist = cadastro.tabela_vigente_em(tid, "2026-08-15")
    assert hist is not None, "deveria achar a vigência arquivada"
    precos_old, prazos_old = hist
    linha_old = precos_old["df"][precos_old["df"]["UF"] == "SP"].iloc[0]
    assert float(linha_old["f20"]) == 100.0, linha_old["f20"]
    assert not prazos_old.empty

    # CTe emitido DEPOIS do reajuste → tabela atual se aplica (None = usa overlay atual = 130).
    assert cadastro.tabela_vigente_em(tid, "2026-09-15") is None

    # Histórico lista a vigência arquivada com o intervalo certo.
    vig = cadastro.list_vigencias(tid)
    assert vig["vigencia_atual_inicio"] == "2026-09-01"
    assert len(vig["arquivadas"]) == 1
    assert vig["arquivadas"][0]["vigencia_fim"] == "2026-08-31"
    print("OK vigência: arquiva/aplica por data de emissão, histórico e intervalos")


if __name__ == "__main__":
    test_vigencia_tabela()
    print("\nTESTE DE VIGÊNCIA (#2) PASSOU.")
