# -*- coding: utf-8 -*-
"""Testes de paginação server-side (limit/offset/total) das listas do LogHub.

Cobre os três endpoints que passaram a paginar:
  - access_log.listar / resumo   (log de acesso)
  - history.query_history / count_history (histórico de auditoria)
  - coletas.painel               (coletas em fornecedores)

Roda com pytest (`python -m pytest tests/test_paginacao.py`) ou direto
(`python tests/test_paginacao.py`). Usa bancos SQLite temporários por env, sem
tocar nos dados reais.
"""
import os
import sys
import tempfile
from pathlib import Path

# Bancos temporários ANTES de importar os módulos (eles leem a env no import).
_TMP = tempfile.mkdtemp(prefix="loghub_pag_")
os.environ["AUTH_DB"] = os.path.join(_TMP, "auth.db")
os.environ["AUDIT_HISTORY_DB"] = os.path.join(_TMP, "audit_history.db")
os.environ["COLETAS_DB"] = os.path.join(_TMP, "coletas.db")
os.environ["VENDAS_DB"] = os.path.join(_TMP, "vendas.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import access_log, history, coletas, vendas  # noqa: E402


def test_access_log_paginacao():
    access_log.init_db()
    for i in range(250):
        access_log.registrar("LOGIN_OK", login=f"user{i}", detalhe=f"evento {i}")
    for i in range(20):
        access_log.registrar("LOGIN_FALHA", login="x", detalhe="falha")

    agg = access_log.resumo()
    assert agg["total"] == 270, agg
    assert agg["por_evento"]["LOGIN_OK"] == 250
    assert agg["por_evento"]["LOGIN_FALHA"] == 20

    # Página respeita o limite e o offset, sem sobreposição.
    p1 = access_log.listar(limit=100, offset=0)
    p2 = access_log.listar(limit=100, offset=100)
    p3 = access_log.listar(limit=100, offset=200)
    assert (len(p1), len(p2), len(p3)) == (100, 100, 70), (len(p1), len(p2), len(p3))
    ids = {r["id"] for r in p1} | {r["id"] for r in p2} | {r["id"] for r in p3}
    assert len(ids) == 270  # todas distintas → nenhuma linha repetida entre páginas

    # Filtro reflete em listar e em resumo (o mesmo WHERE).
    assert access_log.resumo(evento="LOGIN_FALHA")["total"] == 20
    assert len(access_log.listar(evento="LOGIN_FALHA", limit=1000)) == 20
    print("OK access_log: total/kpis/offset/sem-overlap/filtro")


def _seed_history(n, aprovacao="AUTOMATICA"):
    conn = history._get_conn()
    for i in range(n):
        conn.execute(
            "INSERT INTO audit_history (chave_cte, numero_cte, data_emissao, mes_referencia, "
            "status, aprovacao_status, auditado_em) VALUES (?,?,?,?,?,?,?)",
            (f"CH{aprovacao}{i:05d}", str(i), "2026-09-10", "2026-09",
             "OK", aprovacao, "2026-09-10T10:00:00"),
        )
    conn.commit()
    conn.close()


def test_history_paginacao():
    history.init_db()
    _seed_history(150, "AUTOMATICA")
    _seed_history(10, "PENDENTE")

    assert history.count_history() == 160
    p1 = history.query_history(limit=100, offset=0)
    p2 = history.query_history(limit=100, offset=100)
    assert (len(p1), len(p2)) == (100, 60), (len(p1), len(p2))
    chaves = {r["chave_cte"] for r in p1} | {r["chave_cte"] for r in p2}
    assert len(chaves) == 160  # sem sobreposição entre páginas

    # Pendentes no topo (ORDER BY): os 10 pendentes caem na 1ª página.
    pend_p1 = [r for r in p1 if r["aprovacao_status"] == "PENDENTE"]
    assert len(pend_p1) == 10, len(pend_p1)

    # Filtro reflete em count e em query.
    assert history.count_history(aprovacao="PENDENTE") == 10
    assert len(history.query_history(aprovacao="PENDENTE", limit=1000)) == 10
    print("OK history: total/offset/ordem-pendentes/sem-overlap/filtro")


def test_coletas_painel_paginacao(monkeypatch=None):
    # Testa o fatiamento de painel() sem depender do schema: injeta 130 linhas.
    coletas.init_db()  # tabelas auxiliares (meses/transportadoras) precisam existir
    fake = [{"id": i, "fornecedor": f"F{i}"} for i in range(130)]
    orig = coletas.list_coletas
    coletas.list_coletas = lambda **kw: list(fake)
    try:
        full = coletas.painel()  # sem limit → tudo
        assert full["total"] == 130 and len(full["coletas"]) == 130

        pg = coletas.painel(limit=50, offset=0)
        assert pg["total"] == 130 and len(pg["coletas"]) == 50
        assert pg["coletas"][0]["id"] == 0 and pg["coletas"][-1]["id"] == 49

        pg3 = coletas.painel(limit=50, offset=100)
        assert len(pg3["coletas"]) == 30 and pg3["coletas"][0]["id"] == 100
    finally:
        coletas.list_coletas = orig
    print("OK coletas: total/slice/offset (agregados sobre o conjunto inteiro)")


def test_vendas_produtos_paginacao():
    vendas.init_db()
    conn = vendas._get_conn()
    # 120 SKUs distintos; receita cresce com o índice → ordem por receita importa.
    for i in range(120):
        conn.execute(
            "INSERT INTO itens (pedido_id, sku, descricao, quantidade, valor_unitario, bucket, canal) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"P{i}", f"SKU{i:04d}", f"Produto {i}", 1, float(i + 1), "faturado", "ML"),
        )
    conn.commit(); conn.close()

    assert vendas.count_produtos() == 120
    p1 = vendas.produtos(limit=50, offset=0)
    p2 = vendas.produtos(limit=50, offset=50)
    assert (len(p1), len(p2)) == (50, 50)
    # Ordenado por receita DESC: o 1º da página é o de maior valor_unitario (SKU0119).
    assert p1[0]["sku"] == "SKU0119", p1[0]["sku"]
    # Sem sobreposição entre páginas.
    assert not ({r["sku"] for r in p1} & {r["sku"] for r in p2})
    print("OK vendas.produtos: total/offset/ordem-por-receita/sem-overlap")


if __name__ == "__main__":
    test_access_log_paginacao()
    test_history_paginacao()
    test_coletas_painel_paginacao()
    test_vendas_produtos_paginacao()
    print("\nTODOS OS TESTES DE PAGINAÇÃO PASSARAM.")
