# -*- coding: utf-8 -*-
"""Testes dos alertas da conferência (#2): CTe duplicado entre faturas e CTe órfão
(cobrado sem auditoria). DB temporário por env."""
import os
import sys
import tempfile
from pathlib import Path

_T = tempfile.mkdtemp(prefix="loghub_conf_")
os.environ["AUDIT_HISTORY_DB"] = os.path.join(_T, "audit_history.db")
os.environ["CONFERENCIA_DB"] = os.path.join(_T, "conferencia.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import history, conferencia  # noqa: E402

AUDITADO = "1" * 44   # CTe que existe na auditoria
FANTASMA = "2" * 44   # CTe que a fatura cobra mas não está na auditoria


def _seed_auditoria():
    history.init_db()
    conn = history._get_conn()
    conn.execute(
        "INSERT INTO audit_history (chave_cte, numero_cte, data_emissao, mes_referencia, "
        "status, aprovacao_status, auditado_em) VALUES (?,?,?,?,?,?,?)",
        (AUDITADO, "111", "2026-09-10", "2026-09", "OK", "AUTOMATICA", "2026-09-10T10:00:00"))
    conn.commit(); conn.close()


def test_alertas_duplicado_e_orfao():
    _seed_auditoria()
    conferencia.init_db()
    # CTe fantasma cobrado em DUAS faturas → duplicado + órfão.
    conferencia.create_fatura("Jamef", "F-100", "2026-09", "f100.xlsx",
                              [{"chave": FANTASMA, "numero": "222", "valor_fatura": 300.0}])
    conferencia.create_fatura("Jamef", "F-101", "2026-09", "f101.xlsx",
                              [{"chave": FANTASMA, "numero": "222", "valor_fatura": 300.0}])
    # CTe auditado, cobrado uma vez → não é órfão nem duplicado.
    conferencia.create_fatura("Jamef", "F-102", "2026-09", "f102.xlsx",
                              [{"chave": AUDITADO, "numero": "111", "valor_fatura": 150.0}])

    a = conferencia.alertas_conferencia()

    # Duplicado: o CTe fantasma aparece em 2 faturas; o valor repetido = 1 cobrança extra.
    assert len(a["duplicados"]) == 1, a["duplicados"]
    dup = a["duplicados"][0]
    assert dup["chave_cte"] == FANTASMA and dup["vezes"] == 2 and dup["faturas"] == 2
    assert dup["valor_repetido"] == 300.0
    assert a["valor_duplicado_total"] == 300.0

    # Órfão: as 2 linhas do CTe fantasma (não auditado); o auditado NÃO entra.
    chaves_orfas = {o["chave_cte"] for o in a["orfaos"]}
    assert chaves_orfas == {FANTASMA}, chaves_orfas
    assert len(a["orfaos"]) == 2 and a["valor_orfao_total"] == 600.0
    print("OK conferência: duplicado entre faturas + órfão (cobrado sem auditoria)")


if __name__ == "__main__":
    test_alertas_duplicado_e_orfao()
    print("\nTESTE DE ALERTAS DA CONFERÊNCIA (#2) PASSOU.")
