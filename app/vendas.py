"""
Análise de Vendas (Comercial) — puxa os PEDIDOS DE VENDA do Olist/Tiny.

Por que um cache local (e não "ao vivo" a cada acesso):
  A situação e o valor de cada pedido vêm baratos na busca (`pedidos.pesquisa.php`),
  mas o CANAL DE VENDA (Mercado Livre, Shopee, Magalu, Venda Direta) e os ITENS/
  preços só existem no DETALHE (`pedido.obter.php`) — uma chamada por pedido.
  Analisar meses ao vivo seria centenas de chamadas por acesso à tela e esbarraria
  no limite da API. Então sincronizamos os pedidos para um banco local (`vendas.db`),
  em segundo plano + botão "Sincronizar agora", e o painel lê desse cache — sempre
  exibindo a hora da última sincronização.

Comissão: a API de pedidos NÃO expõe o valor de comissão do marketplace. O painel
  trabalha com COMISSÃO ESTIMADA por canal (percentual editável, em
  `tables/vendas_comissoes.json`), aplicada sobre o faturamento — sempre rotulada
  como "estimada". A "Venda Direta" (pedido incluído direto no Olist, sem plataforma)
  entra como canal próprio, com comissão padrão 0%.

Config (variáveis de ambiente / .env):
  VENDAS_JANELA_MESES   janela de histórico a sincronizar (default 6)
  VENDAS_SYNC_MINUTOS   intervalo do auto-sync em minutos (default 15)
  VENDAS_AUTO           liga/desliga o auto-sync (default 1)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app import olist

logger = logging.getLogger("vendas")

_ROOT = Path(__file__).parent.parent
DB_PATH = os.getenv("VENDAS_DB", str(_ROOT / "tables" / "vendas.db"))
_COMISSOES_FILE = Path(os.getenv("VENDAS_COMISSOES", str(_ROOT / "tables" / "vendas_comissoes.json")))
_METAS_FILE = Path(os.getenv("VENDAS_METAS", str(_ROOT / "tables" / "vendas_metas.json")))
_STATE_FILE = Path(os.getenv("VENDAS_STATE", str(_ROOT / "tables" / "vendas_sync.json")))
_IGNORAR_FILE = Path(os.getenv("VENDAS_IGNORAR", str(_ROOT / "tables" / "vendas_ignorar.json")))

# Pedidos de TESTE/homologação a excluir das análises. Os marketplaces injetam
# pedidos de certificação (ex.: o Magalu manda um pedido do cliente "Treinamento
# Luiza Labs") que existem no Olist mas não são vendas reais. Editável no JSON.
IGNORAR_PADRAO: Dict[str, list] = {
    "ids": [],
    "clientes_contendo": ["treinamento luiza labs"],
}


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
def janela_meses() -> int:
    try:
        return max(1, int(os.getenv("VENDAS_JANELA_MESES", "6")))
    except ValueError:
        return 6


def intervalo_min() -> int:
    try:
        return max(5, int(os.getenv("VENDAS_SYNC_MINUTOS", "15")))
    except ValueError:
        return 15


def auto_ligado() -> bool:
    return (os.getenv("VENDAS_AUTO", "1") or "").strip().lower() not in ("0", "false", "nao", "não", "")


# Comissão estimada padrão por canal (%). Editável na tela / no JSON.
COMISSOES_PADRAO: Dict[str, float] = {
    "Mercado Livre": 16.0,
    "Shopee": 14.0,
    "Magalu": 18.0,
    "Magalu Marketplace": 18.0,
    "Amazon": 15.0,
    "Americanas": 16.0,
    "Venda Direta": 0.0,
    "default": 12.0,
}

# Metas de faturamento mktplace por mês (valor líquido + qtde de pedidos faturados).
# Editáveis na tela / em vendas_metas.json. Base: Volix, expectativa de vendas 2026.
METAS_PADRAO: Dict[str, Dict[str, float]] = {
    "2026-08": {"valor": 155000.0, "pedidos": 0},
    "2026-09": {"valor": 630000.0, "pedidos": 252},
    "2026-10": {"valor": 1180000.0, "pedidos": 472},
    "2026-11": {"valor": 4200000.0, "pedidos": 1680},
    "2026-12": {"valor": 4200000.0, "pedidos": 1680},
}


# Palavras-chave que marcam uma natureza de operação como NÃO-VENDA (não entra no
# faturamento): remessa/troca em garantia, bonificação, comodato, demonstração etc.
# A NF de venda normal é "Venda de mercadorias...". Editável em vendas_naturezas.json.
NAO_VENDA_PADRAO = ["garantia", "remessa", "bonific", "comodato", "demonstra",
                    "brinde", "amostra", "conserto", "reparo"]
# EXCEÇÕES que CONTAM como venda mesmo batendo no bloqueio acima. "Remessa em garantia"
# é uma TROCA feita ao cliente (ele fica com o produto reposto) — conta no faturamento
# e na comissão. Tem precedência sobre NAO_VENDA_PADRAO. Editável em vendas_naturezas.json.
VENDA_CONTENDO_PADRAO = ["remessa em garantia"]
_NATUREZAS_FILE = Path(os.getenv("VENDAS_NATUREZAS", str(_ROOT / "tables" / "vendas_naturezas.json")))


def _ler_naturezas_cfg() -> Dict:
    if _NATUREZAS_FILE.exists():
        try:
            dados = json.loads(_NATUREZAS_FILE.read_text(encoding="utf-8"))
            if isinstance(dados, dict):
                return dados
            if isinstance(dados, list):   # formato antigo (só a lista de não-venda)
                return {"nao_venda_contendo": dados}
        except Exception as e:
            logger.warning(f"Falha ao ler naturezas: {e}")
    return {}


def load_nao_venda_keys() -> List[str]:
    ks = _ler_naturezas_cfg().get("nao_venda_contendo")
    if isinstance(ks, list) and ks:
        return [str(k).strip().lower() for k in ks if str(k).strip()]
    return list(NAO_VENDA_PADRAO)


def load_venda_keys() -> List[str]:
    """Naturezas que CONTAM como venda mesmo batendo no bloqueio (ex.: troca em garantia)."""
    ks = _ler_naturezas_cfg().get("venda_contendo")
    if isinstance(ks, list):
        return [str(k).strip().lower() for k in ks if str(k).strip()]
    return list(VENDA_CONTENDO_PADRAO)


def save_nao_venda_keys(keys: List[str], venda_keys: Optional[List[str]] = None) -> List[str]:
    limpo = [str(k).strip() for k in (keys or []) if str(k).strip()]
    vk = venda_keys if venda_keys is not None else load_venda_keys()
    vk = [str(k).strip() for k in vk if str(k).strip()]
    _NATUREZAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _NATUREZAS_FILE.write_text(json.dumps(
        {"nao_venda_contendo": limpo, "venda_contendo": vk}, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_nao_venda_keys()


def _e_venda(natureza: str, keys: Optional[List[str]] = None,
             venda_keys: Optional[List[str]] = None) -> bool:
    """A natureza da operação é uma VENDA (conta como faturamento)? Natureza vazia
    (não resolvida) é tratada como venda para não descartar receita por engano.
    Exceções em `venda_keys` (ex.: 'remessa em garantia') têm precedência sobre o bloqueio."""
    n = (natureza or "").strip().lower()
    if not n:
        return True
    vk = venda_keys if venda_keys is not None else load_venda_keys()
    if any(k in n for k in vk):
        return True
    keys = keys if keys is not None else load_nao_venda_keys()
    return not any(k in n for k in keys)


# Marcas conhecidas p/ derivar a MARCA a partir da DESCRIÇÃO do produto (o item de
# pedido não traz o campo marca). A 1ª que casar (substring, sem acento/caixa) vence;
# sem match => "(outras)". Editável em vendas_marcas.json.
MARCAS_PADRAO = ["Midea", "Springer", "Elgin", "Gree", "TCL", "Samsung", "LG",
                 "Fujitsu", "Daikin", "Carrier", "Philco", "Consul", "Electrolux",
                 "Agratto", "Comfee", "Hitachi", "Panasonic", "York", "Rheem", "Britânia"]
_MARCAS_FILE = Path(os.getenv("VENDAS_MARCAS", str(_ROOT / "tables" / "vendas_marcas.json")))


def load_marcas() -> List[str]:
    if _MARCAS_FILE.exists():
        try:
            d = json.loads(_MARCAS_FILE.read_text(encoding="utf-8"))
            ks = d.get("marcas") if isinstance(d, dict) else d
            if isinstance(ks, list) and ks:
                return [str(k).strip() for k in ks if str(k).strip()]
        except Exception as e:
            logger.warning(f"Falha ao ler marcas: {e}")
    return list(MARCAS_PADRAO)


def save_marcas(marcas: List[str]) -> List[str]:
    limpo = [str(k).strip() for k in (marcas or []) if str(k).strip()]
    _MARCAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MARCAS_FILE.write_text(json.dumps({"marcas": limpo}, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_marcas()


def _marca(descricao: str, marcas: Optional[List[str]] = None) -> str:
    d = (descricao or "").lower()
    for m in (marcas if marcas is not None else load_marcas()):
        if m.lower() in d:
            return m
    return "(outras)"


def reclassificar_venda(conn: sqlite3.Connection) -> int:
    """Recomputa a flag `venda` de todos os pedidos a partir da natureza + regras atuais.
    Roda no init_db p/ aplicar mudanças nas listas venda/não-venda sem precisar re-sincronizar."""
    nao_v, ven = load_nao_venda_keys(), load_venda_keys()
    alterados = 0
    for r in conn.execute("SELECT DISTINCT COALESCE(natureza,'') nat FROM pedidos").fetchall():
        nat = r["nat"]
        v = 1 if _e_venda(nat, nao_v, ven) else 0
        cur = conn.execute(
            "UPDATE pedidos SET venda=? WHERE COALESCE(natureza,'')=? AND COALESCE(venda,-1)!=?",
            (v, nat, v))
        alterados += cur.rowcount
    return alterados


# --------------------------------------------------------------------------- #
# Classificação de situação e canal
# --------------------------------------------------------------------------- #
_CANCELADAS = {"cancelado", "cancelada"}
_ABERTAS = {"em aberto", "aberto", "em separacao", "em separação"}
_AGUARDANDO = {"aprovado", "aguardando faturamento", "em digitacao", "em digitação"}


def _bucket(situacao: str) -> str:
    """Agrupa a situação-texto do Tiny em: faturado / aguardando / aberto / cancelado.

    'faturado' abrange todo pedido concretizado (faturado, enviado, entregue,
    pronto/preparando envio) — é o que compõe o faturamento. 'aguardando' =
    aprovado, ainda sem NF. Situação-texto crua é preservada à parte.
    """
    s = (situacao or "").strip().lower()
    if s in _CANCELADAS:
        return "cancelado"
    if s in _ABERTAS:
        return "aberto"
    if s in _AGUARDANDO:
        return "aguardando"
    return "faturado"


def _canal(ped: Dict) -> str:
    ec = ped.get("ecommerce") or {}
    canal = (ec.get("canalVenda") or ec.get("nomeEcommerce") or "").strip()
    if not canal:
        canal = ((ped.get("intermediador") or {}).get("nome") or "").strip()
    return canal or "Venda Direta"


def load_ignorados() -> Dict[str, list]:
    if _IGNORAR_FILE.exists():
        try:
            dados = json.loads(_IGNORAR_FILE.read_text(encoding="utf-8"))
            if isinstance(dados, dict):
                return {
                    "ids": [str(x) for x in (dados.get("ids") or [])],
                    "clientes_contendo": [str(x) for x in (dados.get("clientes_contendo") or [])],
                }
        except Exception as e:
            logger.warning(f"Falha ao ler lista de ignorados: {e}")
    return {"ids": list(IGNORAR_PADRAO["ids"]), "clientes_contendo": list(IGNORAR_PADRAO["clientes_contendo"])}


def save_ignorados(cfg: Dict[str, list]) -> Dict[str, list]:
    limpo = {
        "ids": [str(x).strip() for x in (cfg.get("ids") or []) if str(x).strip()],
        "clientes_contendo": [str(x).strip() for x in (cfg.get("clientes_contendo") or []) if str(x).strip()],
    }
    _IGNORAR_FILE.parent.mkdir(parents=True, exist_ok=True)
    _IGNORAR_FILE.write_text(json.dumps(limpo, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_ignorados()


def _e_ignorado(pid: str, cliente: str, ig: Dict[str, list]) -> bool:
    """O pedido é de teste/homologação (deve ficar fora das análises)?"""
    if str(pid) in set(ig.get("ids", [])):
        return True
    nome = (cliente or "").strip().lower()
    for pat in ig.get("clientes_contendo", []):
        p = str(pat).strip().lower()
        if p and p in nome:
            return True
    return False


def purgar_ignorados() -> int:
    """Remove do cache os pedidos que hoje batem na lista de ignorados. Retorna
    quantos saíram. Útil para limpar na hora, sem esperar a próxima sync."""
    ig = load_ignorados()
    conn = _get_conn()
    remover = [r["id"] for r in conn.execute("SELECT id, cliente_nome FROM pedidos")
               if _e_ignorado(r["id"], r["cliente_nome"], ig)]
    for pid in remover:
        conn.execute("DELETE FROM pedidos WHERE id = ?", (pid,))
        conn.execute("DELETE FROM itens WHERE pedido_id = ?", (pid,))
    conn.commit()
    conn.close()
    if remover:
        logger.info(f"Vendas: {len(remover)} pedido(s) de teste removido(s) do cache.")
    return len(remover)


def _data_iso(s: str) -> str:
    for f in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((s or "").strip(), f).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    return ""


# --------------------------------------------------------------------------- #
# Banco
# --------------------------------------------------------------------------- #
def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedidos (
            id TEXT PRIMARY KEY,
            numero TEXT,
            numero_ecommerce TEXT,
            data_pedido TEXT,
            data_faturamento TEXT,
            mes TEXT,
            situacao TEXT,
            bucket TEXT,
            canal TEXT,
            cliente_nome TEXT,
            cliente_uf TEXT,
            vendedor TEXT,
            valor_total REAL DEFAULT 0,
            valor_frete REAL DEFAULT 0,
            valor_desconto REAL DEFAULT 0,
            total_produtos REAL DEFAULT 0,
            data_ref TEXT,
            sincronizado_em TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pedido_id TEXT,
            id_produto TEXT,
            sku TEXT,
            descricao TEXT,
            quantidade REAL DEFAULT 0,
            valor_unitario REAL DEFAULT 0,
            data_pedido TEXT,
            data_ref TEXT,
            mes TEXT,
            canal TEXT,
            bucket TEXT
        )
    """)
    # Notas de DEVOLUÇÃO (NF de entrada, finalidade 4). Base do faturamento líquido.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS devolucoes (
            id TEXT PRIMARY KEY,
            numero TEXT,
            data_emissao TEXT,
            data_ref TEXT,
            mes TEXT,
            cliente_nome TEXT,
            valor REAL DEFAULT 0,
            chave TEXT,
            natureza TEXT,
            classificacao TEXT,   -- descontavel | origem_cancelada | nao_atribuida
            canal TEXT,
            pedido_ref TEXT,
            sincronizado_em TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devol_mes ON devolucoes(mes)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devol_dataref ON devolucoes(data_ref)")
    # Migração idempotente: 'data_ref' (data de competência = faturamento ou pedido)
    # foi adicionada depois; garante a coluna em bancos já existentes e faz o backfill.
    _add_column(conn, "pedidos", "data_ref", "TEXT")
    _add_column(conn, "itens", "data_ref", "TEXT")
    # Natureza da operação (vem da NF vinculada) + flag 'venda' (0 = garantia/remessa/
    # bonificação etc., não conta como faturamento). Colunas adicionadas depois.
    _add_column(conn, "pedidos", "id_natureza", "TEXT")
    _add_column(conn, "pedidos", "natureza", "TEXT")
    _add_column(conn, "pedidos", "venda", "INTEGER")
    # 'devolvido' = pedido que tem nota de devolução casada. Vira situação "Devolvido"
    # (separada de Cancelado) e sai do faturamento. Marcado por _classificar_devolucoes.
    _add_column(conn, "pedidos", "devolvido", "INTEGER")
    # 'tem_nf' = a NF de venda foi emitida (id_nota_fiscal). É o que define FATURAMENTO
    # (bate com a base fiscal): NF emitida conta, mesmo que o pedido depois vire Cancelado.
    _add_column(conn, "pedidos", "tem_nf", "INTEGER")
    # 'id_nota_fiscal' = id da NF de venda no Tiny (p/ buscar o NÚMERO da NF sob demanda);
    # 'nf_numero' = número da NF de venda (cacheado ao ligar a devolução à Torre de Controle).
    _add_column(conn, "pedidos", "id_nota_fiscal", "TEXT")
    _add_column(conn, "pedidos", "nf_numero", "TEXT")
    # Devoluções: 'origem_mes'/'origem_valor' = mês e valor da NF de venda casada (para
    # mostrar se a devolução é do mesmo mês ou do mês anterior — a NFD abate no mês dela).
    _add_column(conn, "devolucoes", "origem_mes", "TEXT")
    _add_column(conn, "devolucoes", "origem_valor", "REAL")
    # 'torre_status' = resultado da marcação na Torre de Controle (devolvido/nao_encontrado).
    _add_column(conn, "devolucoes", "torre_status", "TEXT")
    conn.execute("""UPDATE pedidos SET data_ref = COALESCE(NULLIF(data_faturamento,''), data_pedido)
                    WHERE data_ref IS NULL OR data_ref = ''""")
    conn.execute("""UPDATE itens SET data_ref = (
                        SELECT COALESCE(NULLIF(p.data_faturamento,''), p.data_pedido)
                        FROM pedidos p WHERE p.id = itens.pedido_id)
                    WHERE data_ref IS NULL OR data_ref = ''""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_mes ON pedidos(mes)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_dataref ON pedidos(data_ref)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_canal ON pedidos(canal)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_itens_sku ON itens(sku)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_itens_pedido ON itens(pedido_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_itens_mes ON itens(mes)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_itens_dataref ON itens(data_ref)")
    # Reaplica a classificação venda/não-venda (ex.: 'remessa em garantia' passou a contar)
    # sem exigir re-sync — recomputa a flag a partir da natureza já gravada.
    reclassificar_venda(conn)
    conn.commit()
    conn.close()


def _add_column(conn: sqlite3.Connection, tabela: str, coluna: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}
    if coluna not in cols:
        conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {decl}")


def _resolver_natureza(ped: Dict, cache: Dict[str, str]) -> str:
    """Nome da natureza da operação do pedido. Vem da NF vinculada (`id_nota_fiscal`);
    o pedido só traz o id. Usa cache por id_natureza p/ não rebaixar a NF a cada vez."""
    idn = str(ped.get("id_natureza_operacao") or "")
    if idn and idn in cache:
        return cache[idn]
    idnf = str(ped.get("id_nota_fiscal") or "")
    nat = ""
    if idnf and idnf != "0":
        try:
            nf = json.loads(olist._post("nota.fiscal.obter.php", {"id": idnf})).get("retorno", {}).get("nota_fiscal", {})
            nat = (nf.get("natureza_operacao") or "").strip()
        except Exception:
            nat = ""
    if idn and nat:
        cache[idn] = nat
    return nat


def _upsert_pedido(conn: sqlite3.Connection, ped: Dict, busca: Dict, natureza: str = "") -> None:
    """Grava/atualiza um pedido (detalhe do Tiny) e substitui seus itens."""
    pid = str(ped.get("id") or busca.get("id"))
    data_ped = _data_iso(ped.get("data_pedido") or busca.get("data_pedido") or "")
    data_fat = _data_iso(ped.get("data_faturamento") or "")
    # Competência = mês da DATA DE FATURAMENTO (o faturamento do mês conta pela data
    # em que o pedido foi faturado, não pela data em que foi gerado). Sem faturamento
    # ainda (aberto/aguardando/cancelado), cai para a data do pedido.
    data_ref = data_fat or data_ped          # data de competência (usada por mês e por período DE/ATÉ)
    mes = data_ref[:7] if data_ref else ""
    situacao = (ped.get("situacao") or busca.get("situacao") or "").strip()
    bucket = _bucket(situacao)
    canal = _canal(ped)
    cli = ped.get("cliente") or {}
    valor_total = olist._num(ped.get("total_pedido")) or olist._num(busca.get("valor")) or 0.0
    id_natureza = str(ped.get("id_natureza_operacao") or "")
    venda = 1 if _e_venda(natureza) else 0
    idnf = str(ped.get("id_nota_fiscal") or "")
    tem_nf = 1 if (idnf and idnf != "0") else 0

    conn.execute("DELETE FROM pedidos WHERE id = ?", (pid,))
    conn.execute("""
        INSERT INTO pedidos (id, numero, numero_ecommerce, data_pedido, data_faturamento, mes,
                             situacao, bucket, canal, cliente_nome, cliente_uf, vendedor,
                             valor_total, valor_frete, valor_desconto, total_produtos, data_ref,
                             id_natureza, natureza, venda, tem_nf, id_nota_fiscal, sincronizado_em)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        pid, ped.get("numero") or busca.get("numero"), ped.get("numero_ecommerce") or busca.get("numero_ecommerce"),
        data_ped, data_fat, mes,
        situacao, bucket, canal, cli.get("nome") or busca.get("nome"), (cli.get("uf") or "").strip().upper(),
        (ped.get("nome_vendedor") or busca.get("nome_vendedor") or "").strip(),
        valor_total, olist._num(ped.get("valor_frete")) or 0.0, olist._num(ped.get("valor_desconto")) or 0.0,
        olist._num(ped.get("total_produtos")) or 0.0, data_ref,
        id_natureza, natureza, venda, tem_nf, idnf, datetime.now().isoformat(timespec="seconds"),
    ))

    conn.execute("DELETE FROM itens WHERE pedido_id = ?", (pid,))
    for x in (ped.get("itens") or []):
        i = x.get("item", x)
        conn.execute("""
            INSERT INTO itens (pedido_id, id_produto, sku, descricao, quantidade, valor_unitario,
                               data_pedido, data_ref, mes, canal, bucket)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pid, str(i.get("id_produto") or ""), (i.get("codigo") or "").strip(), i.get("descricao"),
            olist._num(i.get("quantidade")) or 0.0, olist._num(i.get("valor_unitario")) or 0.0,
            data_ped, data_ref, mes, canal, bucket,
        ))


# --------------------------------------------------------------------------- #
# Devoluções (NF de entrada, finalidade 4) -> faturamento líquido
# --------------------------------------------------------------------------- #
def _norm_nome(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _e_devolucao(det: Dict) -> bool:
    """A NF de entrada é uma DEVOLUÇÃO de venda? (finalidade 4 ou natureza 'devolução')."""
    if str(det.get("finalidade") or "").strip() == "4":
        return True
    return "devol" in (det.get("natureza_operacao") or "").lower()


def _upsert_devolucao(conn: sqlite3.Connection, det: Dict) -> None:
    did = str(det.get("id"))
    data = _data_iso(det.get("data_emissao") or "")
    valor = olist._num(det.get("valor_nota")) or olist._num(det.get("valor")) or 0.0
    cli = det.get("cliente") or {}
    conn.execute("DELETE FROM devolucoes WHERE id = ?", (did,))
    conn.execute("""
        INSERT INTO devolucoes (id, numero, data_emissao, data_ref, mes, cliente_nome, valor, chave,
                                natureza, classificacao, canal, pedido_ref, sincronizado_em)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        did, det.get("numero"), data, data, data[:7] if data else "",
        cli.get("nome"), valor, det.get("chave_acesso"),
        (det.get("natureza_operacao") or "").strip(),
        None, None, None, datetime.now().isoformat(timespec="seconds"),
    ))


def _sincronizar_devolucoes(conn: sqlite3.Connection, ini: str, fim: str) -> int:
    """Varre as NF de ENTRADA da janela e grava as de devolução. Só detalha notas
    ainda não inspecionadas (guarda o resultado no estado) — sync incremental barato."""
    estado = _carregar_estado()
    inspec = estado.get("entrada_inspecionadas", {})  # id -> 1 (devolução) / 0 (compra)
    novas = 0
    pagina = 1
    while True:
        txt = olist._post("notas.fiscais.pesquisa.php",
                          {"dataInicial": ini, "dataFinal": fim, "tipoNota": "E", "pagina": str(pagina)})
        ret = json.loads(txt).get("retorno", {})
        if str(ret.get("status", "")).upper() != "OK":
            break
        for it in ret.get("notas_fiscais", []):
            nf = it.get("nota_fiscal", it)
            nid = str(nf.get("id"))
            if nid in inspec:
                continue  # já sabemos o que é (e, se devolução, já está na tabela)
            det = json.loads(olist._post("nota.fiscal.obter.php", {"id": nid})).get("retorno", {}).get("nota_fiscal", {})
            if _e_devolucao(det):
                _upsert_devolucao(conn, det)
                inspec[nid] = 1
                novas += 1
            else:
                inspec[nid] = 0
        try:
            npag = int(ret.get("numero_paginas") or 1)
        except (TypeError, ValueError):
            npag = 1
        if pagina >= npag:
            break
        pagina += 1
    estado["entrada_inspecionadas"] = inspec
    _salvar_estado(estado)
    return novas


def _classificar_devolucoes(conn: sqlite3.Connection) -> None:
    """Liga cada devolução ao pedido de origem (por cliente + valor) e marca o pedido
    como DEVOLVIDO (vira situação 'Devolvido', sai do faturamento). A devolução fica:
      - devolvido     : casou com um pedido -> aquele pedido é marcado devolvido=1
      - nao_atribuida : sem pedido correspondente -> desconta em 'Não atribuído'
    Toda devolução aparece como 'Devolvido' no painel (a distinção é só p/ a conta:
    pedido que era faturado sai do faturamento; pedido que já era cancelado já estava fora)."""
    # zera as marcações (o casamento pode mudar entre syncs) e reindexa por cliente
    conn.execute("UPDATE pedidos SET devolvido = 0")
    por_nome: Dict[str, list] = {}
    for r in conn.execute("SELECT id, numero, canal, bucket, valor_total, data_ref, cliente_nome, COALESCE(venda,1) venda FROM pedidos"):
        por_nome.setdefault(_norm_nome(r["cliente_nome"]), []).append(dict(r))

    usados: set = set()
    for d in conn.execute("SELECT id, cliente_nome, valor FROM devolucoes ORDER BY valor DESC").fetchall():
        cands = [c for c in por_nome.get(_norm_nome(d["cliente_nome"]), []) if c["id"] not in usados]
        # preferência: venda faturada > qualquer faturado > qualquer pedido; empata por |valor|
        faturados_venda = [c for c in cands if c["bucket"] == "faturado" and c["venda"] == 1]
        faturados = [c for c in cands if c["bucket"] == "faturado"]
        pool = faturados_venda or faturados or cands
        if pool:
            best = min(pool, key=lambda c: abs((c["valor_total"] or 0) - (d["valor"] or 0)))
            usados.add(best["id"])
            conn.execute("UPDATE pedidos SET devolvido = 1 WHERE id = ?", (best["id"],))
            classif, canal, ref = "devolvido", best["canal"], best["numero"]
            o_mes, o_val = (best["data_ref"] or "")[:7], best["valor_total"] or 0.0
        else:
            classif, canal, ref, o_mes, o_val = "nao_atribuida", "Não atribuído", None, None, None
        conn.execute("UPDATE devolucoes SET classificacao=?, canal=?, pedido_ref=?, origem_mes=?, origem_valor=? WHERE id=?",
                     (classif, canal, ref, o_mes, o_val, d["id"]))


def _obter_numero_nf(id_nota_fiscal: str) -> str:
    """Número da NF de venda a partir do id da NF no Tiny (nota.fiscal.obter)."""
    try:
        nf = json.loads(olist._post("nota.fiscal.obter.php", {"id": str(id_nota_fiscal)})
                        ).get("retorno", {}).get("nota_fiscal", {})
        return str(nf.get("numero") or "").strip()
    except Exception as e:
        logger.warning(f"Falha ao obter número da NF {id_nota_fiscal}: {e}")
        return ""


def _marcar_devolvidos_torre(conn: sqlite3.Connection) -> Dict:
    """Fecha o ciclo com a Torre de Controle: para cada devolução CASADA, descobre o
    NÚMERO da NF de venda (cacheia em pedidos.nf_numero) e marca a entrega daquela NF
    como DEVOLVIDA na Torre, se ainda não estiver. Idempotente. Falha de API não quebra
    a sync (a devolução fica com torre_status='pendente' e tenta de novo na próxima)."""
    from app import tracking  # import tardio evita qualquer ordem de import
    res = {"marcadas": 0, "ja_devolvidas": 0, "nao_encontradas": 0, "sem_nf": 0}
    rows = conn.execute(
        "SELECT d.id did, p.id pid, p.nf_numero nfnum, p.id_nota_fiscal idnf "
        "FROM devolucoes d JOIN pedidos p ON p.numero = d.pedido_ref "
        "WHERE d.classificacao='devolvido' AND COALESCE(d.pedido_ref,'')!=''").fetchall()
    for r in rows:
        numero = (r["nfnum"] or "").strip()
        if not numero:
            idnf = (r["idnf"] or "").strip()
            if not idnf or idnf == "0":
                try:
                    ped = olist.obter_pedido(r["pid"])
                    idnf = str(ped.get("id_nota_fiscal") or "")
                except Exception as e:
                    logger.warning(f"Falha ao obter pedido {r['pid']} p/ NF: {e}")
                    idnf = ""
            if idnf and idnf != "0":
                numero = _obter_numero_nf(idnf)
                if numero:
                    conn.execute("UPDATE pedidos SET nf_numero=?, id_nota_fiscal=? WHERE id=?",
                                 (numero, idnf, r["pid"]))
        if not numero:
            res["sem_nf"] += 1
            conn.execute("UPDATE devolucoes SET torre_status='pendente' WHERE id=?", (r["did"],))
            continue
        out = tracking.marcar_devolvida_por_nf(numero)   # numero_nf único p/ o emissor único da Torre
        if out["encontradas"] == 0:
            res["nao_encontradas"] += 1
            st = "nao_encontrada"
        elif out["marcadas"] > 0:
            res["marcadas"] += out["marcadas"]
            st = "devolvido"
        else:
            res["ja_devolvidas"] += 1
            st = "ja_devolvido"
        conn.execute("UPDATE devolucoes SET torre_status=? WHERE id=?", (st, r["did"]))
    return res


# --------------------------------------------------------------------------- #
# Sincronização
# --------------------------------------------------------------------------- #
def sincronizar(meses: Optional[int] = None, force: bool = False) -> Dict:
    """Varre os pedidos da janela e grava no cache local.

    Otimização: um pedido só é detalhado (chamada extra à API) se for novo, se a
    situação mudou desde a última sync, ou se ainda não tem itens gravados. Assim
    as sincronizações seguintes são baratas — só pega o que mudou.
    """
    if not olist.configurado():
        return {"ok": False, "erro": "Token do Olist não configurado. Defina OLIST_TOKEN no arquivo .env."}

    inicio = time.time()
    meses = meses or janela_meses()
    hoje = datetime.now()
    dt_ini = hoje - timedelta(days=meses * 31)
    ini = dt_ini.strftime("%d/%m/%Y")
    fim = hoje.strftime("%d/%m/%Y")
    cutoff_iso = dt_ini.strftime("%Y-%m-%d")  # p/ reconciliar exclusões dentro da janela

    ig = load_ignorados()
    nat_cache = _carregar_estado().get("naturezas", {})  # id_natureza -> nome (evita rebaixar NF)
    conn = _get_conn()
    existentes = {r["id"]: r["situacao"] for r in conn.execute("SELECT id, situacao FROM pedidos")}
    com_itens = {r["pedido_id"] for r in conn.execute("SELECT DISTINCT pedido_id FROM itens")}

    novos = atualizados = detalhados = vistos = ignorados = removidos = dev_novas = 0
    vistos_ids: set = set()   # todos os ids retornados pela busca (p/ detectar exclusões)
    paginou_ok = False        # houve ao menos uma página OK? (evita limpar cache em erro transitório)
    erros: List[Dict] = []
    pagina = 1
    try:
        while True:
            ret = olist.pesquisar_pedidos_pagina(ini, fim, pagina)
            if str(ret.get("status", "")).upper() != "OK":
                break  # "Erro" costuma ser só "nenhum registro" no período
            paginou_ok = True
            for it in ret.get("pedidos", []):
                p = it.get("pedido", it)
                pid = str(p.get("id"))
                vistos += 1
                vistos_ids.add(pid)
                # Pedido de teste/homologação (ex.: "Treinamento Luiza Labs") — fora
                # das análises. Já filtra pela busca (economiza a chamada de detalhe)
                # e limpa se por acaso já estava no cache.
                if _e_ignorado(pid, p.get("nome"), ig):
                    ignorados += 1
                    if pid in existentes:
                        conn.execute("DELETE FROM pedidos WHERE id = ?", (pid,))
                        conn.execute("DELETE FROM itens WHERE pedido_id = ?", (pid,))
                    continue
                sit = (p.get("situacao") or "").strip()
                precisa = force or pid not in existentes or existentes[pid] != sit or pid not in com_itens
                if not precisa:
                    continue
                try:
                    ped = olist.obter_pedido(pid)
                    natureza = _resolver_natureza(ped, nat_cache)
                    _upsert_pedido(conn, ped, p, natureza)
                    detalhados += 1
                    if pid in existentes:
                        atualizados += 1
                    else:
                        novos += 1
                except Exception as e:  # um pedido problemático não derruba a sync
                    erros.append({"pedido": p.get("numero") or pid, "erro": str(e)})
            try:
                npag = int(ret.get("numero_paginas") or 1)
            except (TypeError, ValueError):
                npag = 1
            if pagina >= npag:
                break
            pagina += 1

        # Reconciliação de EXCLUSÕES: pedido que existe no cache dentro da janela mas
        # não voltou na busca foi EXCLUÍDO no Olist (a busca traz todas as situações).
        # Só roda se a varredura teve ao menos uma página OK — evita limpar o cache
        # por uma resposta vazia/erro transitório da API.
        removidos = 0
        if paginou_ok:
            orfaos = [r["id"] for r in conn.execute(
                "SELECT id FROM pedidos WHERE data_pedido >= ? AND data_pedido != ''", (cutoff_iso,))
                if r["id"] not in vistos_ids]
            for pid in orfaos:
                conn.execute("DELETE FROM pedidos WHERE id = ?", (pid,))
                conn.execute("DELETE FROM itens WHERE pedido_id = ?", (pid,))
            removidos = len(orfaos)
            if removidos:
                logger.info(f"Vendas: {removidos} pedido(s) removido(s) do cache (excluídos no Olist).")

        # Notas de devolução (NF de entrada, finalidade 4) — para o faturamento líquido.
        try:
            dev_novas = _sincronizar_devolucoes(conn, ini, fim)
        except Exception as e:
            logger.error(f"Vendas: falha ao sincronizar devoluções: {e}")
            dev_novas = 0
        # (Re)classifica todas as devoluções contra o estado atual dos pedidos.
        _classificar_devolucoes(conn)
        # Fecha o ciclo: reflete "devolvido" na Torre de Controle (NF de venda casada).
        try:
            torre = _marcar_devolvidos_torre(conn)
        except Exception as e:
            logger.error(f"Vendas: falha ao marcar devolvidos na Torre: {e}")
            torre = {"marcadas": 0, "ja_devolvidas": 0, "nao_encontradas": 0, "sem_nf": 0}
        conn.commit()
    finally:
        conn.close()

    resumo = {
        "vistos": vistos,
        "novos": novos,
        "atualizados": atualizados,
        "detalhados": detalhados,
        "ignorados": ignorados,
        "removidos": removidos,
        "devolucoes_novas": dev_novas,
        "torre_devolvidas": torre.get("marcadas", 0),
        "torre_ja_devolvidas": torre.get("ja_devolvidas", 0),
        "torre_nao_encontradas": torre.get("nao_encontradas", 0),
        "erros": len(erros),
        "segundos": round(time.time() - inicio, 1),
    }
    estado = _carregar_estado()
    estado["ultima_sync"] = datetime.now().isoformat(timespec="seconds")
    estado["ultimo_resultado"] = resumo
    estado["naturezas"] = nat_cache
    _salvar_estado(estado)
    return {"ok": True, **resumo, "erros_detalhe": erros}


# --------------------------------------------------------------------------- #
# Estado (última sincronização)
# --------------------------------------------------------------------------- #
def _carregar_estado() -> Dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _salvar_estado(estado: Dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Falha ao salvar estado de vendas: {e}")


# --------------------------------------------------------------------------- #
# Comissões estimadas por canal
# --------------------------------------------------------------------------- #
def load_comissoes() -> Dict[str, float]:
    if _COMISSOES_FILE.exists():
        try:
            dados = json.loads(_COMISSOES_FILE.read_text(encoding="utf-8"))
            if isinstance(dados, dict):
                base = dict(COMISSOES_PADRAO)
                base.update({k: float(v) for k, v in dados.items()})
                return base
        except Exception as e:
            logger.warning(f"Falha ao ler comissões: {e}")
    return dict(COMISSOES_PADRAO)


def save_comissoes(taxas: Dict[str, float]) -> Dict[str, float]:
    limpo = {}
    for k, v in (taxas or {}).items():
        try:
            limpo[str(k)] = round(float(v), 4)
        except (TypeError, ValueError):
            continue
    _COMISSOES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COMISSOES_FILE.write_text(json.dumps(limpo, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_comissoes()


def _comissao_pct(canal: str, taxas: Dict[str, float]) -> float:
    if canal in taxas:
        return taxas[canal]
    alvo = (canal or "").strip().lower()
    for k, v in taxas.items():
        if k == "default":
            continue
        if alvo and (alvo in k.lower() or k.lower() in alvo):
            return v
    return taxas.get("default", 0.0)


# --------------------------------------------------------------------------- #
# Metas de faturamento (mktplace) por mês
# --------------------------------------------------------------------------- #
def load_metas() -> Dict[str, Dict[str, float]]:
    """Metas por mês ('YYYY-MM' -> {valor, pedidos}). Arquivo sobrepõe o padrão."""
    metas = {m: dict(v) for m, v in METAS_PADRAO.items()}
    if _METAS_FILE.exists():
        try:
            dados = json.loads(_METAS_FILE.read_text(encoding="utf-8"))
            if isinstance(dados, dict):
                for m, v in dados.items():
                    if isinstance(v, dict):
                        cur = metas.get(m, {"valor": 0.0, "pedidos": 0})
                        if "valor" in v:
                            cur["valor"] = float(v.get("valor") or 0)
                        if "pedidos" in v:
                            cur["pedidos"] = int(float(v.get("pedidos") or 0))
                        metas[m] = cur
        except Exception as e:
            logger.warning(f"Falha ao ler metas: {e}")
    return dict(sorted(metas.items()))


def save_metas(metas: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    limpo: Dict[str, Dict[str, float]] = {}
    for m, v in (metas or {}).items():
        m = str(m).strip()
        if len(m) != 7 or m[4] != "-":  # espera 'YYYY-MM'
            continue
        if not isinstance(v, dict):
            continue
        try:
            limpo[m] = {"valor": round(float(v.get("valor") or 0), 2),
                        "pedidos": int(float(v.get("pedidos") or 0))}
        except (TypeError, ValueError):
            continue
    _METAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _METAS_FILE.write_text(json.dumps(limpo, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_metas()


def _meses_do_periodo(mes: Optional[str], de: Optional[str], ate: Optional[str],
                      metas: Dict[str, Dict[str, float]]) -> List[str]:
    """Meses ('YYYY-MM') abrangidos pelo filtro, restritos aos que têm meta.

    DE/ATÉ tem precedência (recorta pelo prefixo YYYY-MM). Sem filtro = todos os
    meses com meta cadastrada."""
    todos = sorted(metas.keys())
    if de or ate:
        d = (de or "")[:7]
        a = (ate or "")[:7]
        return [m for m in todos if (not d or m >= d) and (not a or m <= a)]
    if mes:
        return [m for m in todos if m == mes]
    return todos


# --------------------------------------------------------------------------- #
# Consultas / analytics
# --------------------------------------------------------------------------- #
def _periodo_conds(mes: Optional[str], de: Optional[str], ate: Optional[str],
                   prefix: str = "") -> Tuple[list, list]:
    """Condições SQL do filtro de período, sobre a data de competência (`data_ref`).

    Intervalo DE/ATÉ tem precedência sobre o mês; se nenhum for dado, sem filtro.
    `prefix` permite qualificar as colunas quando há alias na query (ex.: 'i.')."""
    dref = f"{prefix}data_ref"
    mcol = f"{prefix}mes"
    conds: list = []
    params: list = []
    if de:
        conds.append(f"{dref} >= ?"); params.append(de)
    if ate:
        conds.append(f"{dref} <= ?"); params.append(ate)
    if not (de or ate) and mes:
        conds.append(f"{mcol} = ?"); params.append(mes)
    return conds, params


def list_meses() -> List[str]:
    conn = _get_conn()
    rows = conn.execute("SELECT DISTINCT mes FROM pedidos WHERE mes != '' ORDER BY mes DESC")
    out = [r["mes"] for r in rows]
    conn.close()
    return out


def resumo(mes: Optional[str] = None, de: Optional[str] = None, ate: Optional[str] = None) -> Dict:
    conn = _get_conn()
    conds, params = _periodo_conds(mes, de, ate)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    # 'devbucket' = bucket p/ exibição: pedido com devolução casada vira 'devolvido'
    # (situação própria, separada de Cancelado). Usado nas situações e nas contagens.
    DEVB = "CASE WHEN COALESCE(devolvido,0)=1 THEN 'devolvido' ELSE bucket END"
    DEVSIT = "CASE WHEN COALESCE(devolvido,0)=1 THEN 'Devolvido' ELSE situacao END"

    # Situações — todas, com contagem e valor (devolvido aparece à parte)
    por_situacao = [
        {"situacao": r["sit"] or "—", "bucket": r["bk"], "pedidos": r["n"], "valor": r["v"] or 0.0}
        for r in conn.execute(
            f"SELECT {DEVSIT} sit, {DEVB} bk, COUNT(*) n, COALESCE(SUM(valor_total),0) v "
            f"FROM pedidos {where} GROUP BY sit ORDER BY v DESC", params)
    ]

    # Totais por bucket (devolvido separado de cancelado)
    bk = {r["bk"]: {"pedidos": r["n"], "valor": r["v"] or 0.0}
          for r in conn.execute(
              f"SELECT {DEVB} bk, COUNT(*) n, COALESCE(SUM(valor_total),0) v FROM pedidos {where} GROUP BY bk", params)}

    total_pedidos = sum(b["pedidos"] for b in bk.values())

    # ── Devoluções (NF de entrada, finalidade 4) EMITIDAS no período ──────────
    # REGRA: a NF de devolução (NFD) abate no MÊS EM QUE FOI EMITIDA. Tanto faz se a
    # venda de origem foi no mesmo mês ou no mês anterior — o abatimento cai sempre no
    # mês da devolução (por isso a NFD é filtrada pela própria emissão, `data_ref`).
    # Cada NFD é casada com a NF de venda por NOME + valor só para atribuir o canal (e
    # exibir o par); o abatimento é o VALOR DA NFD, não a remoção da venda de origem.
    dconds, dparams = _periodo_conds(mes, de, ate)
    dwhere = ("WHERE " + " AND ".join(dconds)) if dconds else ""
    dev_total_notas = 0.0
    dev_qtd = 0
    dev_por_canal: Dict[str, float] = {}
    for r in conn.execute(
        f"SELECT COALESCE(NULLIF(canal,''),'Não atribuído') canal, COUNT(*) n, COALESCE(SUM(valor),0) v "
        f"FROM devolucoes {dwhere} GROUP BY canal", dparams
    ):
        dev_por_canal[r["canal"]] = dev_por_canal.get(r["canal"], 0.0) + (r["v"] or 0.0)
        dev_total_notas += r["v"] or 0.0
        dev_qtd += r["n"]

    # Faturamento = NF de venda emitida (data_faturamento OU tem_nf). BRUTO inclui TUDO
    # que foi faturado no período — inclusive vendas que depois tiveram devolução; a NFD
    # abate à parte (acima), no mês da emissão. Só VENDA (garantia/remessa ficam fora).
    FAT = "(COALESCE(data_faturamento,'')!='' OR COALESCE(tem_nf,0)=1)"
    fat_where = f"WHERE {FAT}" + ((" AND " + " AND ".join(conds)) if conds else "")
    fr = conn.execute(
        f"SELECT COALESCE(SUM(CASE WHEN COALESCE(venda,1)=1 THEN valor_total ELSE 0 END),0) fbruto, "
        f"COALESCE(SUM(CASE WHEN COALESCE(venda,1)=1 THEN 1 ELSE 0 END),0) nvv, "
        f"COALESCE(SUM(CASE WHEN COALESCE(venda,1)=0 THEN valor_total ELSE 0 END),0) fnv, "
        f"COALESCE(SUM(CASE WHEN COALESCE(venda,1)=0 THEN 1 ELSE 0 END),0) nnv "
        f"FROM pedidos {fat_where}", params).fetchone()
    faturamento_bruto = fr["fbruto"] or 0.0            # todas as vendas faturadas (BRUTO)
    faturamento_nao_venda = fr["fnv"] or 0.0
    n_faturados = fr["nvv"] or 0
    faturamento_liquido = faturamento_bruto - dev_total_notas   # BRUTO − NFD do mês
    kpis = {
        "total_pedidos": total_pedidos,
        "faturados": n_faturados,
        "devolvidos": bk.get("devolvido", {}).get("pedidos", 0),
        "aguardando": bk.get("aguardando", {}).get("pedidos", 0),
        "abertos": bk.get("aberto", {}).get("pedidos", 0),
        "cancelados": bk.get("cancelado", {}).get("pedidos", 0),
        "faturamento_nao_venda": faturamento_nao_venda,
        "nao_venda_qtd": fr["nnv"] or 0,
        "valor_cancelado": bk.get("cancelado", {}).get("valor", 0.0),
        "valor_aguardando": bk.get("aguardando", {}).get("valor", 0.0),  # em aberto p/ faturar
    }

    # Faturamento por natureza da operação (BRUTO — todas as vendas faturadas)
    por_natureza = [
        {"natureza": r["nat"], "venda": bool(r["venda"]), "pedidos": r["n"], "valor": r["v"] or 0.0}
        for r in conn.execute(
            f"SELECT COALESCE(NULLIF(natureza,''),'(não informada)') nat, COALESCE(venda,1) venda, "
            f"COUNT(*) n, COALESCE(SUM(valor_total),0) v FROM pedidos {fat_where} GROUP BY nat ORDER BY v DESC", params)
    ]

    # Faturamento por canal: BRUTO (venda faturada) − devoluções do mês (por canal) = líquido.
    taxas = load_comissoes()
    por_canal = []
    dev_rest = dict(dev_por_canal)   # o que sobrar (canal sem venda no período) vira linha própria
    for r in conn.execute(
        f"SELECT canal, COUNT(*) n, COALESCE(SUM(valor_total),0) v, "
        f"COALESCE(SUM(CASE WHEN {FAT} AND COALESCE(venda,1)=1 THEN valor_total ELSE 0 END),0) fbruto, "
        f"COALESCE(SUM(CASE WHEN {FAT} AND COALESCE(venda,1)=1 THEN 1 ELSE 0 END),0) nfat "
        f"FROM pedidos {where} GROUP BY canal ORDER BY fbruto DESC", params
    ):
        fbruto = r["fbruto"] or 0.0
        devc = dev_rest.pop(r["canal"], 0.0)
        fnet = fbruto - devc
        pct = _comissao_pct(r["canal"], taxas)
        comissao = fnet * pct / 100.0
        por_canal.append({
            "canal": r["canal"], "pedidos": r["n"], "faturados": r["nfat"],
            "valor_total": r["v"] or 0.0, "faturamento": fbruto,
            "devolucoes": devc, "faturamento_liquido": fnet,
            "ticket_medio": (fbruto / r["nfat"]) if r["nfat"] else 0.0,
            "comissao_pct": pct, "comissao_valor": comissao, "liquido": fnet - comissao,
        })
    # devoluções de canal sem venda no período (ou 'Não atribuído') = linha própria (só abate)
    for canal, devc in dev_rest.items():
        if not devc:
            continue
        por_canal.append({
            "canal": canal, "pedidos": 0, "faturados": 0, "valor_total": 0.0,
            "faturamento": 0.0, "ticket_medio": 0.0, "comissao_pct": 0.0, "comissao_valor": 0.0,
            "liquido": -devc, "participacao": 0.0,
            "devolucoes": devc, "faturamento_liquido": -devc,
        })
    part_total = faturamento_liquido or 1.0
    for c in por_canal:
        c.setdefault("participacao", c["faturamento_liquido"] / part_total * 100.0)

    devolucoes_lista = [
        {"numero": r["numero"], "data": r["data_ref"], "cliente": r["cliente_nome"],
         "valor": r["valor"] or 0.0, "canal": r["canal"], "classificacao": r["classificacao"],
         "pedido_ref": r["pedido_ref"], "origem_mes": r["origem_mes"], "torre_status": r["torre_status"]}
        for r in conn.execute(
            f"SELECT numero, data_ref, cliente_nome, valor, canal, classificacao, pedido_ref, origem_mes, torre_status "
            f"FROM devolucoes {dwhere} ORDER BY data_ref DESC, valor DESC", dparams)
    ]

    # Naturezas de operação com QTDE DE NF por natureza (todas as NF do período):
    # vendas/remessas (NF de saída, pela emissão) + devoluções (NF de entrada).
    naturezas_nf = []
    for r in conn.execute(
        f"SELECT COALESCE(NULLIF(natureza,''),'(não informada)') nat, COALESCE(venda,1) venda, "
        f"COUNT(*) n, COALESCE(SUM(valor_total),0) v FROM pedidos {fat_where} GROUP BY nat", params
    ):
        naturezas_nf.append({"natureza": r["nat"], "tipo": ("venda" if r["venda"] else "nao_venda"),
                             "nfs": r["n"], "valor": r["v"] or 0.0})
    for r in conn.execute(
        f"SELECT COALESCE(NULLIF(natureza,''),'(NF-e) Devolução') nat, COUNT(*) n, COALESCE(SUM(valor),0) v "
        f"FROM devolucoes {dwhere} GROUP BY nat", dparams
    ):
        naturezas_nf.append({"natureza": r["nat"], "tipo": "devolucao", "nfs": r["n"], "valor": r["v"] or 0.0})
    naturezas_nf.sort(key=lambda x: -x["nfs"])

    kpis["faturamento_bruto"] = faturamento_bruto
    kpis["faturamento_total"] = faturamento_liquido
    kpis["faturamento_liquido"] = faturamento_liquido
    kpis["ticket_medio"] = (faturamento_liquido / n_faturados) if n_faturados else 0.0
    kpis["devolucoes"] = dev_total_notas            # total das NFD do mês
    kpis["devolucoes_qtd"] = dev_qtd                # qtde de NFD no mês
    kpis["devolucoes_impacto"] = dev_total_notas    # abatimento aplicado ao faturamento

    # Evolução mensal (independe do filtro de mês) — faturamento de VENDA
    evolucao = [
        {"mes": r["mes"], "faturamento": r["fat"] or 0.0, "pedidos": r["n"]}
        for r in conn.execute(
            "SELECT mes, COALESCE(SUM(CASE WHEN (COALESCE(data_faturamento,'')!='' OR COALESCE(tem_nf,0)=1) AND COALESCE(venda,1)=1 AND COALESCE(devolvido,0)=0 THEN valor_total ELSE 0 END),0) fat, "
            "COUNT(*) n FROM pedidos WHERE mes != '' GROUP BY mes ORDER BY mes")
    ]

    # ── Metas (mktplace) por mês: meta × realizado líquido e pedidos faturados ──
    metas = load_metas()
    # realizado por mês (todos os meses; independe do filtro), consistente com o KPI:
    # BRUTO do mês (todas as vendas faturadas) − NFD emitidas no mês.
    real_valor: Dict[str, float] = {}
    real_ped: Dict[str, int] = {}
    for r in conn.execute(
        f"SELECT mes, COALESCE(SUM(CASE WHEN {FAT} AND COALESCE(venda,1)=1 THEN valor_total ELSE 0 END),0) v, "
        f"COALESCE(SUM(CASE WHEN {FAT} AND COALESCE(venda,1)=1 THEN 1 ELSE 0 END),0) n "
        f"FROM pedidos WHERE mes != '' GROUP BY mes"):
        real_valor[r["mes"]] = r["v"] or 0.0
        real_ped[r["mes"]] = r["n"] or 0
    for r in conn.execute(
        "SELECT mes, COALESCE(SUM(valor),0) v FROM devolucoes WHERE mes != '' GROUP BY mes"):
        real_valor[r["mes"]] = real_valor.get(r["mes"], 0.0) - (r["v"] or 0.0)

    def _pct(real, meta):
        return round(real / meta * 100.0, 1) if meta else None

    meta_linhas = []
    for m in sorted(metas.keys()):
        mv = metas[m].get("valor", 0.0) or 0.0
        mp = int(metas[m].get("pedidos", 0) or 0)
        rv = real_valor.get(m, 0.0)
        rp = real_ped.get(m, 0)
        meta_linhas.append({
            "mes": m, "meta_valor": mv, "meta_pedidos": mp,
            "real_valor": rv, "real_pedidos": rp,
            "pct_valor": _pct(rv, mv), "pct_pedidos": _pct(rp, mp),
        })

    meses_periodo = _meses_do_periodo(mes, de, ate, metas)
    meta_valor_periodo = sum((metas[m].get("valor", 0.0) or 0.0) for m in meses_periodo)
    meta_ped_periodo = sum(int(metas[m].get("pedidos", 0) or 0) for m in meses_periodo)
    meta = {
        "base": "liquido",
        "linhas": meta_linhas,
        "meses_periodo": meses_periodo,
        "meta_valor": meta_valor_periodo,
        "meta_pedidos": meta_ped_periodo,
        "real_valor": faturamento_liquido,
        "real_pedidos": n_faturados,
        "pct_valor": _pct(faturamento_liquido, meta_valor_periodo),
        "pct_pedidos": _pct(n_faturados, meta_ped_periodo),
    }

    conn.close()
    return {
        "mes": mes,
        "de": de,
        "ate": ate,
        "kpis": kpis,
        "meta": meta,
        "por_situacao": por_situacao,
        "por_natureza": por_natureza,
        "naturezas_nf": naturezas_nf,
        "por_canal": por_canal,
        "devolucoes": devolucoes_lista,
        "evolucao": evolucao,
        "comissao_nota": "Comissão estimada por canal (a API do pedido não fornece o valor real).",
        "devolucao_nota": "Faturamento líquido = bruto − devoluções de pedidos ainda faturados.",
        "sync": status(),
    }


def _produtos_where(mes, de, ate, marca=None):
    """WHERE compartilhado por produtos()/count_produtos() — mesmo filtro na
    página e na contagem de SKUs distintos. `marca` filtra pela descrição."""
    cond = ["bucket != 'cancelado'", "sku != ''"]
    pconds, params = _periodo_conds(mes, de, ate)
    cond += pconds
    if marca:
        if marca == "(outras)":  # não bate em nenhuma marca conhecida
            kws = load_marcas()
            if kws:
                cond.append("NOT (" + " OR ".join(["LOWER(descricao) LIKE ?"] * len(kws)) + ")")
                params += [f"%{k.lower()}%" for k in kws]
        else:
            cond.append("LOWER(descricao) LIKE ?")
            params.append(f"%{marca.lower()}%")
    return "WHERE " + " AND ".join(cond), list(params)


def count_produtos(mes: Optional[str] = None, de: Optional[str] = None,
                   ate: Optional[str] = None, marca: Optional[str] = None) -> int:
    """Quantos SKUs distintos vendidos no período (base do pager)."""
    where, params = _produtos_where(mes, de, ate, marca)
    conn = _get_conn()
    total = conn.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM itens {where} GROUP BY sku)", params).fetchone()[0]
    conn.close()
    return total


def vendas_por_marca(mes: Optional[str] = None, de: Optional[str] = None,
                     ate: Optional[str] = None) -> List[Dict]:
    """Quanto vendeu de cada MARCA no período (exclui cancelados): quantidade,
    receita, nº de pedidos, SKUs distintos e participação. Marca derivada da descrição."""
    where, params = _produtos_where(mes, de, ate)
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT pedido_id, descricao, sku, COALESCE(quantidade,0) q, "
        f"COALESCE(quantidade*valor_unitario,0) r FROM itens {where}", params).fetchall()
    conn.close()
    marcas = load_marcas()
    agg: Dict[str, Dict] = {}
    for x in rows:
        m = _marca(x["descricao"], marcas)
        a = agg.setdefault(m, {"marca": m, "qtd": 0.0, "receita": 0.0, "pedidos": set(), "skus": set()})
        a["qtd"] += x["q"]; a["receita"] += x["r"]
        a["pedidos"].add(x["pedido_id"]); a["skus"].add(x["sku"])
    total = sum(a["receita"] for a in agg.values()) or 1.0
    out = [{"marca": a["marca"], "qtd": a["qtd"], "receita": a["receita"],
            "pedidos": len(a["pedidos"]), "skus": len(a["skus"]),
            "participacao": a["receita"] / total * 100.0} for a in agg.values()]
    out.sort(key=lambda z: -z["receita"])
    return out


def produtos(mes: Optional[str] = None, de: Optional[str] = None, ate: Optional[str] = None,
             limit: int = 100, offset: int = 0, marca: Optional[str] = None) -> List[Dict]:
    """Produtos vendidos no período (exclui cancelados), do maior para o menor
    faturamento. Preço médio ponderado (receita/qtd), mínimo, máximo e variação %
    (amplitude sobre o mínimo). Ordenação e paginação resolvidas no SQL. `marca` filtra."""
    where, params = _produtos_where(mes, de, ate, marca)
    conn = _get_conn()
    rows = conn.execute(f"""
        SELECT sku, MAX(descricao) descricao,
               COALESCE(SUM(quantidade),0) qtd,
               COALESCE(SUM(quantidade*valor_unitario),0) receita,
               COUNT(DISTINCT pedido_id) pedidos,
               MIN(valor_unitario) preco_min, MAX(valor_unitario) preco_max,
               COUNT(DISTINCT canal) canais
        FROM itens {where} GROUP BY sku ORDER BY receita DESC LIMIT ? OFFSET ?
    """, params + [max(1, int(limit)), max(0, int(offset or 0))])
    marcas = load_marcas()
    out = []
    for r in rows:
        qtd = r["qtd"] or 0.0
        pmin, pmax = r["preco_min"] or 0.0, r["preco_max"] or 0.0
        variacao = ((pmax - pmin) / pmin * 100.0) if pmin else 0.0
        out.append({
            "sku": r["sku"], "descricao": r["descricao"], "marca": _marca(r["descricao"], marcas),
            "qtd": qtd, "receita": r["receita"] or 0.0, "pedidos": r["pedidos"],
            "preco_medio": (r["receita"] / qtd) if qtd else 0.0,
            "preco_min": pmin, "preco_max": pmax, "variacao_pct": variacao,
            "canais": r["canais"],
        })
    conn.close()
    return out


def preco_historico(sku: str, mes: Optional[str] = None, de: Optional[str] = None,
                    ate: Optional[str] = None) -> Dict:
    """Série de preços de um SKU ao longo do tempo (para a análise de variação de
    preço) + preço médio por canal. Cada ponto = um item de pedido não cancelado."""
    conn = _get_conn()
    cond = ["i.sku = ?", "i.bucket != 'cancelado'"]
    pconds, pparams = _periodo_conds(mes, de, ate, prefix="i.")
    cond += pconds
    params: list = [sku] + list(pparams)
    where = "WHERE " + " AND ".join(cond)
    pontos = [
        {"data": r["data_pedido"], "mes": r["mes"], "canal": r["canal"],
         "preco": r["valor_unitario"] or 0.0, "quantidade": r["quantidade"] or 0.0,
         "pedido": r["numero"]}
        for r in conn.execute(f"""
            SELECT i.data_pedido, i.mes, i.canal, i.valor_unitario, i.quantidade, p.numero
            FROM itens i LEFT JOIN pedidos p ON p.id = i.pedido_id
            {where} ORDER BY i.data_pedido, i.id
        """, params)
    ]
    por_canal = [
        {"canal": r["canal"], "preco_medio": (r["rec"] / r["q"]) if r["q"] else 0.0,
         "preco_min": r["mn"] or 0.0, "preco_max": r["mx"] or 0.0, "qtd": r["q"] or 0.0}
        for r in conn.execute(f"""
            SELECT i.canal, COALESCE(SUM(i.quantidade*i.valor_unitario),0) rec,
                   COALESCE(SUM(i.quantidade),0) q, MIN(i.valor_unitario) mn, MAX(i.valor_unitario) mx
            FROM itens i {where} GROUP BY i.canal ORDER BY rec DESC
        """, params)
    ]
    desc = conn.execute("SELECT descricao FROM itens WHERE sku = ? AND descricao IS NOT NULL LIMIT 1", (sku,)).fetchone()
    conn.close()
    precos = [p["preco"] for p in pontos if p["preco"]]
    return {
        "sku": sku,
        "descricao": desc["descricao"] if desc else sku,
        "pontos": pontos,
        "por_canal": por_canal,
        "preco_min": min(precos) if precos else 0.0,
        "preco_max": max(precos) if precos else 0.0,
        "variacao_pct": ((max(precos) - min(precos)) / min(precos) * 100.0) if precos and min(precos) else 0.0,
    }


def status() -> Dict:
    estado = _carregar_estado()
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) n FROM pedidos").fetchone()["n"]
    conn.close()
    return {
        "configurado": olist.configurado(),
        "auto": auto_ligado(),
        "intervalo_min": intervalo_min(),
        "janela_meses": janela_meses(),
        "pedidos_no_cache": total,
        "ignorar": load_ignorados(),
        "ultima_sync": estado.get("ultima_sync"),
        "ultimo_resultado": estado.get("ultimo_resultado"),
    }


# --------------------------------------------------------------------------- #
# Auto-sync em segundo plano
# --------------------------------------------------------------------------- #
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop() -> None:
    _stop.wait(35)  # deixa o servidor subir (e não colide com o auto-sync do romaneio)
    while not _stop.is_set():
        try:
            if olist.configurado() and auto_ligado():
                res = sincronizar()
                if res.get("ok") and (res.get("novos") or res.get("atualizados")):
                    logger.info(f"Vendas auto-sync: {res['novos']} novo(s), {res['atualizados']} atualizado(s).")
        except Exception as e:
            logger.error(f"Vendas auto-sync falhou: {e}")
        _stop.wait(intervalo_min() * 60)


def iniciar_auto() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    if not olist.configurado():
        logger.info("Vendas: token do Olist não configurado — auto-sync desligado.")
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="vendas-sync", daemon=True)
    _thread.start()
    logger.info(f"Vendas: auto-sync ligado (a cada {intervalo_min()} min, janela de {janela_meses()} meses).")


def parar_auto() -> None:
    _stop.set()
