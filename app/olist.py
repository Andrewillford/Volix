"""
Integração com o Olist ERP (Tiny ERP) — API 2.0.

Puxa os XMLs das NF-e emitidas no Olist e joga na FILA do Romaneio,
disparando o ciclo de expedição sem digitação manual.

Fluxo:
  1) notas.fiscais.pesquisa.php  -> lista as NF-e de SAÍDA do período (com chave = autorizadas)
  2) nota.fiscal.obter.xml.php   -> baixa o XML cru de cada NF nova
  3) romaneio.add_notas_from_xml -> entra na fila (dedupe por chave já existente)

Token: definido em OLIST_TOKEN (variável de ambiente ou arquivo .env na raiz).
NUNCA é commitado — .env é gitignored. O token não aparece em logs.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from app import romaneio as romaneio_mod

logger = logging.getLogger("olist")

_ROOT = Path(__file__).parent.parent
_ENV_FILE = _ROOT / ".env"
_STATE_FILE = Path(os.getenv("OLIST_STATE", str(_ROOT / "tables" / "olist_sync.json")))

API_BASE = os.getenv("OLIST_API_BASE", "https://api.tiny.com.br/api2/")
TIMEOUT = int(os.getenv("OLIST_TIMEOUT", "30"))


# --------------------------------------------------------------------------- #
# Configuração (.env)
# --------------------------------------------------------------------------- #
def _load_env() -> None:
    """Carrega as chaves do .env para o ambiente (sem sobrescrever o que já existe)."""
    if not _ENV_FILE.exists():
        return
    try:
        for linha in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            chave = chave.strip()
            valor = valor.strip().strip('"').strip("'")
            if chave and chave not in os.environ:
                os.environ[chave] = valor
    except Exception as e:  # nunca deixa o .env derrubar o app
        logger.warning(f"Falha ao ler .env: {e}")


_load_env()


def get_token() -> str:
    return (os.getenv("OLIST_TOKEN") or "").strip()


def configurado() -> bool:
    return bool(get_token())


def auto_ligado() -> bool:
    return (os.getenv("OLIST_AUTO", "1") or "").strip().lower() not in ("0", "false", "nao", "não", "")


def intervalo_min() -> int:
    try:
        return max(2, int(os.getenv("OLIST_SYNC_MINUTOS", "15")))
    except ValueError:
        return 15


def janela_dias() -> int:
    """Quantos dias pra trás a sincronização varre para ADICIONAR novas NFs (default: hoje + ontem)."""
    try:
        return max(1, int(os.getenv("OLIST_JANELA_DIAS", "2")))
    except ValueError:
        return 2


def janela_cancelamento_dias() -> int:
    """Janela (mais ampla) para detectar NFs CANCELADAS e estornar (default: 30 dias)."""
    try:
        return max(1, int(os.getenv("OLIST_CANCELAMENTO_DIAS", "30")))
    except ValueError:
        return 30


def gate_expedicao_aberta() -> bool:
    """DESLIGADO por padrão: a API v2 do Olist não expõe se um agrupamento está
    pendente ou concluído (todas as expedições agrupadas voltam situacao=1), então
    o filtro "só expedição aberta" não é confiável. Ligue com OLIST_SO_EXPEDICAO_ABERTA=1
    apenas se aceitar a limitação (só considera aberta a expedição ainda NÃO agrupada)."""
    return (os.getenv("OLIST_SO_EXPEDICAO_ABERTA", "0") or "").strip().lower() in ("1", "true", "sim")


# --------------------------------------------------------------------------- #
# Chamadas à API do Tiny/Olist
# --------------------------------------------------------------------------- #
def _post(endpoint: str, params: Dict[str, str]) -> str:
    dados = {"token": get_token(), "formato": "json"}
    dados.update(params)
    corpo = urllib.parse.urlencode(dados).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + endpoint,
        data=corpo,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _pesquisar_pagina(data_inicial: str, data_final: str, pagina: int) -> Dict:
    params = {
        "dataInicial": data_inicial,
        "dataFinal": data_final,
        "tipoNota": "S",       # só notas de SAÍDA (expedição)
        "pagina": str(pagina),
    }
    txt = _post("notas.fiscais.pesquisa.php", params)
    return json.loads(txt).get("retorno", {})


def _e_cancelada(nf: Dict) -> bool:
    """A NF está cancelada? (pela descrição da situação que o Olist retorna)."""
    return "cancel" in (nf.get("descricao_situacao") or "").strip().lower()


def listar_notas(dias: Optional[int] = None) -> List[Dict]:
    """NF-e de saída com chave de acesso do período (inclui as canceladas, marcadas)."""
    dias = dias or janela_dias()
    hoje = datetime.now()
    ini = (hoje - timedelta(days=max(0, dias - 1))).strftime("%d/%m/%Y")
    fim = hoje.strftime("%d/%m/%Y")

    achadas: List[Dict] = []
    pagina = 1
    while True:
        ret = _pesquisar_pagina(ini, fim, pagina)
        if str(ret.get("status", "")).upper() != "OK":
            break  # "Erro" costuma ser apenas "nenhum registro" no período
        for item in ret.get("notas_fiscais", []):
            nf = item.get("nota_fiscal", item)
            chave = re.sub(r"\D", "", nf.get("chave_acesso") or "")
            cancelada = _e_cancelada(nf)
            if len(chave) == 44:
                achadas.append({"id": str(nf.get("id")), "chave": chave,
                                "numero": nf.get("numero"), "cancelada": cancelada,
                                "data": nf.get("data_emissao")})
            elif cancelada:
                # NF cancelada perde a chave na busca — guarda o id p/ recuperá-la depois
                achadas.append({"id": str(nf.get("id")), "chave": "",
                                "numero": nf.get("numero"), "cancelada": True,
                                "data": nf.get("data_emissao")})
        try:
            npag = int(ret.get("numero_paginas") or 1)
        except (TypeError, ValueError):
            npag = 1
        if pagina >= npag:
            break
        pagina += 1
    return achadas


def obter_xml(id_nota: str) -> Optional[str]:
    txt = _post("nota.fiscal.obter.xml.php", {"id": str(id_nota)})
    # Caminho normal: envelope JSON com xml_nfe como string.
    try:
        ret = json.loads(txt).get("retorno", {})
        if str(ret.get("status", "")).upper() == "OK":
            return ret.get("xml_nfe")
        return None
    except (json.JSONDecodeError, AttributeError):
        # Fallback: envelope veio como XML — extrai <xml_nfe> (com ou sem CDATA).
        m = re.search(r"<xml_nfe>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</xml_nfe>", txt, re.DOTALL)
        return m.group(1) if m else None


def obter_chave(id_nota: str) -> str:
    """Recupera a chave de acesso pelo XML da NF.

    Necessário para NFs CANCELADAS: a busca (e o obter.php) devolvem a chave
    em branco quando a nota está cancelada, mas o XML ainda a contém.
    """
    try:
        xml = obter_xml(id_nota) or ""
    except Exception:
        xml = ""
    m = re.search(r"NFe(\d{44})", xml)
    return m.group(1) if m else ""


def _num(v) -> Optional[float]:
    """Converte um valor da API (ex.: '5.25', '', None) em float ou None."""
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def pesquisar_produtos(termo: str, limite: int = 25) -> List[Dict]:
    """Busca produtos no Olist por nome OU código/SKU (mesmo campo `pesquisa`).

    Traz o custo direto (preco_custo / preco_custo_medio) — usado pela
    Calculadora de Margem para preencher o CMV.
    """
    if not configurado():
        raise RuntimeError("Token do Olist não configurado. Defina OLIST_TOKEN no arquivo .env.")
    termo = (termo or "").strip()
    if len(termo) < 2:
        return []
    txt = _post("produtos.pesquisa.php", {"pesquisa": termo})
    ret = json.loads(txt).get("retorno", {})
    if str(ret.get("status", "")).upper() != "OK":
        return []  # "Erro" aqui costuma ser apenas "nenhum registro"
    out: List[Dict] = []
    for item in ret.get("produtos", []):
        p = item.get("produto", item)
        out.append({
            "id": str(p.get("id")),
            "nome": p.get("nome"),
            "sku": p.get("codigo"),
            "preco": _num(p.get("preco")),
            "custo": _num(p.get("preco_custo")),
            "custo_medio": _num(p.get("preco_custo_medio")),
            "unidade": p.get("unidade"),
        })
        if len(out) >= limite:
            break
    return out


# --------------------------------------------------------------------------- #
# Pedidos de Venda (base do painel Comercial / Análise de Vendas)
# --------------------------------------------------------------------------- #
def pesquisar_pedidos_pagina(data_inicial: str, data_final: str, pagina: int) -> Dict:
    """Uma página de `pedidos.pesquisa.php` (retorno bruto do Tiny).

    Traz a lista de pedidos do período com os campos de topo (id, número,
    situação, valor, cliente) — sem os itens. Situação já vem em texto
    ("Faturado", "Cancelado", "Em aberto", ...). O canal de venda e os itens
    só existem no detalhe (`obter_pedido`).
    """
    txt = _post("pedidos.pesquisa.php",
                {"dataInicial": data_inicial, "dataFinal": data_final, "pagina": str(pagina)})
    return json.loads(txt).get("retorno", {})


def obter_pedido(id_pedido: str) -> Dict:
    """Detalhe completo de um pedido (`pedido.obter.php`): itens, canal de
    venda (`ecommerce.canalVenda`), frete, desconto, totais e faturamento."""
    txt = _post("pedido.obter.php", {"id": str(id_pedido)})
    return json.loads(txt).get("retorno", {}).get("pedido", {}) or {}


# --------------------------------------------------------------------------- #
# Custo por SKU a partir das NOTAS DE ENTRADA
# (o custo real não fica no cadastro; para kits = soma dos componentes)
# --------------------------------------------------------------------------- #
_CUSTOS_FILE = Path(os.getenv("OLIST_CUSTOS", str(_ROOT / "tables" / "olist_custos.json")))
_custo_cache: Dict = {"idx": None, "ts": 0.0}


def janela_entradas_dias() -> int:
    try:
        return max(30, int(os.getenv("OLIST_ENTRADAS_DIAS", "730")))
    except ValueError:
        return 730


def custo_ttl_seg() -> int:
    try:
        return max(60, int(os.getenv("OLIST_CUSTO_TTL_MIN", "360")) * 60)
    except ValueError:
        return 360 * 60


def _obter_produto(pid: str) -> Dict:
    txt = _post("produto.obter.php", {"id": str(pid)})
    return json.loads(txt).get("retorno", {}).get("produto", {}) or {}


def _data_nf(s: str) -> datetime:
    for f in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f)
        except (ValueError, TypeError):
            pass
    return datetime.min


def construir_indice_custos() -> Dict[str, Dict]:
    """Varre as notas de ENTRADA e monta {SKU: {custo, data}} — custo da entrada mais recente."""
    if not configurado():
        return {}
    hoje = datetime.now()
    ini = (hoje - timedelta(days=janela_entradas_dias())).strftime("%d/%m/%Y")
    fim = hoje.strftime("%d/%m/%Y")
    idx: Dict[str, Dict] = {}
    pagina = 1
    while True:
        txt = _post("notas.fiscais.pesquisa.php",
                    {"dataInicial": ini, "dataFinal": fim, "tipoNota": "E", "pagina": str(pagina)})
        ret = json.loads(txt).get("retorno", {})
        if str(ret.get("status", "")).upper() != "OK":
            break
        for it in ret.get("notas_fiscais", []):
            nf = it.get("nota_fiscal", it)
            data = _data_nf(nf.get("data_emissao", ""))
            det = json.loads(_post("nota.fiscal.obter.php", {"id": str(nf.get("id"))})).get("retorno", {})
            for x in det.get("nota_fiscal", det).get("itens", []):
                i = x.get("item", x)
                sku = (i.get("codigo") or "").strip()
                if not sku:
                    continue
                custo = _num(i.get("valor_unitario")) or 0.0
                ant = idx.get(sku)
                if ant is None or data >= _data_nf(ant["data"]):
                    idx[sku] = {"custo": custo, "data": data.strftime("%Y-%m-%d")}
        try:
            npag = int(ret.get("numero_paginas") or 1)
        except (TypeError, ValueError):
            npag = 1
        if pagina >= npag:
            break
        pagina += 1
    try:
        _CUSTOS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CUSTOS_FILE.write_text(
            json.dumps({"gerado_em": datetime.now().isoformat(timespec="seconds"), "skus": idx},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Falha ao salvar índice de custos: {e}")
    logger.info(f"Olist: índice de custos das entradas atualizado ({len(idx)} SKUs).")
    return idx


def indice_custos(force: bool = False) -> Dict[str, Dict]:
    agora = time.time()
    if not force and _custo_cache["idx"] is not None and (agora - _custo_cache["ts"]) < custo_ttl_seg():
        return _custo_cache["idx"]
    if not force and _custo_cache["idx"] is None and _CUSTOS_FILE.exists():
        try:
            dados = json.loads(_CUSTOS_FILE.read_text(encoding="utf-8"))
            ger = datetime.fromisoformat(dados.get("gerado_em"))
            if (datetime.now() - ger).total_seconds() < custo_ttl_seg():
                _custo_cache["idx"] = dados.get("skus", {})
                _custo_cache["ts"] = agora
                return _custo_cache["idx"]
        except Exception:
            pass
    idx = construir_indice_custos()
    _custo_cache["idx"] = idx
    _custo_cache["ts"] = agora
    return idx


def custo_do_produto(pid: str) -> Dict:
    """Custo real de um produto pelas notas de entrada. Kit (classe K) = soma dos componentes."""
    if not configurado():
        return {"ok": False, "erro": "Token do Olist não configurado."}
    try:
        idx = indice_custos()
        prod = _obter_produto(pid)
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao consultar o Olist: {e}"}
    if not prod:
        return {"ok": False, "erro": "Produto não encontrado no Olist."}

    is_kit = str(prod.get("classe_produto", "")).upper() == "K"
    componentes: List[Dict] = []
    faltantes: List[str] = []
    total = 0.0

    def _add(sku: str, nome, qtd: float):
        nonlocal total
        reg = idx.get(sku)
        custo = reg["custo"] if reg else None
        if custo is None:
            faltantes.append(sku or "(sem SKU)")
        else:
            total += custo * qtd
        componentes.append({"sku": sku, "nome": nome, "custo_unit": custo, "qtd": qtd})

    if is_kit:
        for c in (prod.get("kit") or []):
            item = c.get("item", c)
            cid = item.get("id_produto")
            qtd = _num(item.get("quantidade")) or 1.0
            comp = _obter_produto(cid) if cid else {}
            _add((comp.get("codigo") or "").strip(), comp.get("nome"), qtd)
    else:
        _add((prod.get("codigo") or "").strip(), prod.get("nome"), 1.0)

    completo = not faltantes
    return {
        "ok": True,
        "id": str(pid),
        "nome": prod.get("nome"),
        "kit": is_kit,
        "custo": round(total, 2) if (completo or total > 0) else None,
        "completo": completo,
        "faltantes": faltantes,
        "componentes": componentes,
        "origem": "notas de entrada",
    }


# --------------------------------------------------------------------------- #
# Expedição do Olist — cria o AGRUPAMENTO (romaneio) a partir de um romaneio LogHub
# --------------------------------------------------------------------------- #
def _so_digitos(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _num_nf(s) -> str:
    """Número da NF sem zeros à esquerda (p/ casar identificacao do Olist com nf_numero)."""
    d = _so_digitos(s)
    return d.lstrip("0") or d


def _erro_txt(ret: Dict) -> str:
    errs = ret.get("erros")
    if isinstance(errs, list) and errs:
        return "; ".join(str(e.get("erro", e) if isinstance(e, dict) else e) for e in errs)
    return ret.get("erro") or "Erro desconhecido no Olist."


def pesquisar_expedicoes(dias: int = 30) -> Dict[str, Dict]:
    """Mapa {numero_nf: expedicao} do período — varre transportadora (T) e Correios (C),
    incluindo não definidas e já agrupadas."""
    hoje = datetime.now()
    ini = (hoje - timedelta(days=dias)).strftime("%d/%m/%Y")
    fim = hoje.strftime("%d/%m/%Y")
    mapa: Dict[str, Dict] = {}
    for fe in ("T", "C"):
        pagina = 1
        while True:
            txt = _post("expedicao.pesquisa.php", {
                "dataInicial": ini, "dataFinal": fim, "formaEnvio": fe,
                "incluirNaoDefinidas": "S", "incluirAgrupadas": "S", "pagina": str(pagina),
            })
            ret = json.loads(txt).get("retorno", {})
            if str(ret.get("status", "")).upper() != "OK":
                break  # "Erro" aqui costuma ser só "nenhum registro"
            for item in ret.get("expedicoes", []):
                e = item.get("expedicao", item)
                num = _num_nf(e.get("identificacao"))
                if num:
                    mapa.setdefault(num, e)
            try:
                npag = int(ret.get("numero_paginas") or 1)
            except (TypeError, ValueError):
                npag = 1
            if pagina >= npag:
                break
            pagina += 1
    return mapa


def _expedicao_aberta(e: Optional[Dict]) -> bool:
    """Expedição pendente de liberação: existe, situacao=0 (pendente) e ainda não agrupada."""
    if not e:
        return False
    if e.get("idAgrupamento"):
        return False  # já agrupada = já entrou num romaneio no Olist
    return str(e.get("situacao")) == "0"


def criar_agrupamento(ids_expedicao: List) -> Dict:
    if not ids_expedicao:
        return {"ok": False, "erro": "Nenhuma expedição para agrupar."}
    txt = _post("expedicao.incluir.agrupamento.php",
                {"idsExpedicao": ",".join(str(i) for i in ids_expedicao)})
    ret = json.loads(txt).get("retorno", {})
    if str(ret.get("status", "")).upper() != "OK":
        return {"ok": False, "erro": _erro_txt(ret)}
    return {"ok": True, "idAgrupamento": ret.get("idAgrupamento")}


def enviar_romaneio_para_expedicao(rid: int) -> Dict:
    """Opção A: cria no Olist o agrupamento (Expedição) equivalente a um romaneio LogHub."""
    if not configurado():
        return {"ok": False, "erro": "Token do Olist não configurado."}
    rom = romaneio_mod.get_romaneio(rid)
    if not rom:
        return {"ok": False, "erro": "Romaneio não encontrado."}
    if rom.get("olist_agrupamento_id"):
        return {"ok": False, "erro": f"Romaneio já enviado ao Olist (agrupamento {rom['olist_agrupamento_id']})."}
    try:
        expes = pesquisar_expedicoes()
    except Exception as e:
        return {"ok": False, "erro": f"Falha ao consultar expedições no Olist: {e}"}

    ids: List = []
    detalhe: List[Dict] = []
    for it in rom.get("itens", []):
        num = _num_nf(it.get("nf_numero"))
        e = expes.get(num)
        if not e:
            detalhe.append({"nf": it.get("nf_numero"), "status": "sem expedição no Olist"})
        elif e.get("idAgrupamento"):
            detalhe.append({"nf": it.get("nf_numero"), "status": f"já agrupada ({e.get('idAgrupamento')})"})
        else:
            ids.append(e.get("id"))
            detalhe.append({"nf": it.get("nf_numero"), "status": "ok", "expedicao": e.get("id")})

    if not ids:
        return {"ok": False, "erro": "Nenhuma expedição elegível para agrupar.", "detalhe": detalhe}

    res = criar_agrupamento(ids)
    if not res.get("ok"):
        return {"ok": False, "erro": res.get("erro"), "detalhe": detalhe}
    agrup = res.get("idAgrupamento")
    romaneio_mod.set_olist_agrupamento(rid, agrup)
    return {"ok": True, "idAgrupamento": agrup, "agrupadas": len(ids), "detalhe": detalhe}


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
        logger.warning(f"Falha ao salvar estado do Olist: {e}")


# --------------------------------------------------------------------------- #
# Sincronização
# --------------------------------------------------------------------------- #
def sincronizar(dias: Optional[int] = None) -> Dict:
    if not configurado():
        return {"ok": False, "erro": "Token do Olist não configurado. Defina OLIST_TOKEN no arquivo .env."}

    inicio = time.time()
    add_dias = dias or janela_dias()
    canc_dias = max(add_dias, janela_cancelamento_dias())
    try:
        candidatas = listar_notas(canc_dias)  # janela ampla: pega adds recentes + cancelamentos tardios
    except Exception as e:
        logger.error(f"Olist: falha ao pesquisar NF-e: {e}")
        return {"ok": False, "erro": f"Falha ao consultar o Olist: {e}"}

    ja_conhecidas = romaneio_mod.chaves_existentes()
    limite_add = (datetime.now() - timedelta(days=add_dias - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Status da Expedição do Olist: a fila só recebe NF com expedição ABERTA (pendente).
    so_aberta = gate_expedicao_aberta()
    expes: Dict[str, Dict] = {}
    if so_aberta:
        try:
            expes = pesquisar_expedicoes(canc_dias)
        except Exception as e:
            logger.warning(f"Olist: falha ao consultar expedições (segue sem o filtro): {e}")
            so_aberta = False

    xmls: List = []
    erros: List[Dict] = []
    estornos: List[Dict] = []
    alertas: List[Dict] = []
    for nf in candidatas:
        # NF cancelada -> estorna se já entrou no LogHub
        if nf.get("cancelada"):
            chave = nf["chave"] or obter_chave(nf["id"])  # cancelada não traz chave na busca
            if chave and chave in ja_conhecidas:
                info = romaneio_mod.marcar_cancelada(chave)
                if info:
                    estornos.append(info)
                    if info.get("em_romaneio"):
                        alertas.append(info)
            continue
        # NF ativa nova -> adiciona
        if nf["chave"] in ja_conhecidas:
            continue
        if so_aberta:
            # só entra quem tem expedição ABERTA no Olist (pendente de liberação, não agrupada)
            if not _expedicao_aberta(expes.get(_num_nf(nf.get("numero")))):
                continue
        else:
            # modo antigo: respeita a janela de adição por data
            d = _data_nf(nf.get("data") or "")
            if d != datetime.min and d < limite_add:
                continue
        try:
            xml = obter_xml(nf["id"])
        except Exception as e:
            erros.append({"nf": nf.get("numero"), "erro": str(e)})
            continue
        if xml:
            xmls.append((f"olist-{nf['chave']}.xml", xml.encode("utf-8")))
        else:
            erros.append({"nf": nf.get("numero"), "erro": "XML não retornado pelo Olist."})

    # Reconciliação: tira da fila as notas cuja expedição JÁ foi fechada/agrupada no Olist.
    fechadas = 0
    if so_aberta:
        for nota in romaneio_mod.notas_na_fila():
            e = expes.get(_num_nf(nota.get("nf_numero")))
            if e is not None and not _expedicao_aberta(e):
                if romaneio_mod.marcar_expedida(nota.get("chave")):
                    fechadas += 1

    resultado = (
        romaneio_mod.add_notas_from_xml(xmls) if xmls
        else {"adicionadas": 0, "duplicadas": 0, "erros": []}
    )

    # Quantas das NF-e encontradas têm expedição ABERTA (candidatas reais para a fila).
    com_exp_aberta = (
        sum(1 for nf in candidatas
            if not nf.get("cancelada") and _expedicao_aberta(expes.get(_num_nf(nf.get("numero")))))
        if so_aberta else None
    )

    resumo = {
        "encontradas": len(candidatas),
        "com_expedicao_aberta": com_exp_aberta,
        "novas": resultado.get("adicionadas", 0),
        "duplicadas": resultado.get("duplicadas", 0),
        "canceladas": len(estornos),
        "fechadas": fechadas,
        "alertas": len(alertas),
        "erros": len(erros) + len(resultado.get("erros", [])),
        "segundos": round(time.time() - inicio, 1),
    }
    estado = _carregar_estado()
    estado["ultima_sync"] = datetime.now().isoformat(timespec="seconds")
    estado["ultimo_resultado"] = resumo
    _salvar_estado(estado)

    detalhe = erros + [
        {"nf": e.get("arquivo"), "erro": e.get("erro")} for e in resultado.get("erros", [])
    ]
    return {"ok": True, **resumo, "erros_detalhe": detalhe,
            "estornos": [{"nf": e["nf_numero"], "em_romaneio": e["em_romaneio"]} for e in estornos],
            "alertas_detalhe": alertas}


def ressincronizar_peso_volume(dias: Optional[int] = None) -> Dict:
    """Completa peso/volume das NFs já cadastradas que entraram sem esses dados.

    Não adiciona nem remove NF nenhuma: para cada nota sem peso/volume, relê o
    XML atual no Olist e atualiza SÓ peso_bruto/peso_liquido/volumes/especie/m3.
    Se o XML autorizado continua sem <vol> (NF autorizada antes de a expedição
    preencher os volumes), a nota entra em `sem_dado` — nesse caso o dado não
    existe na origem e não há o que puxar."""
    inicio = time.time()
    if not configurado():
        return {"ok": False, "erro": "Token do Olist não configurado. Defina OLIST_TOKEN no arquivo .env."}
    from app import nfe_parser

    pendentes = romaneio_mod.notas_sem_peso()
    if not pendentes:
        return {"ok": True, "pendentes": 0, "atualizadas": 0, "sem_dado": 0,
                "nao_encontradas": 0, "erros": 0, "erros_detalhe": [],
                "segundos": round(time.time() - inicio, 1),
                "mensagem": "Nenhuma NF pendente de peso/volume."}

    # Mapa chave -> id do Olist. Janela ampla para cobrir NFs antigas: usa a
    # maior entre a janela padrão e o pedido, com piso de 60 dias.
    janela = max(dias or janela_dias(), 60)
    try:
        candidatas = listar_notas(janela)
    except Exception as e:
        logger.error(f"Olist: falha ao pesquisar NF-e para re-sincronizar: {e}")
        return {"ok": False, "erro": f"Falha ao consultar o Olist: {e}"}
    id_por_chave = {nf["chave"]: nf["id"] for nf in candidatas if nf.get("chave")}

    atualizadas = sem_dado = nao_encontradas = 0
    erros: List[Dict] = []
    for nota in pendentes:
        chave = re.sub(r"\D", "", nota.get("chave") or "")
        id_nota = id_por_chave.get(chave)
        if not id_nota:
            nao_encontradas += 1
            continue
        try:
            xml = obter_xml(id_nota)
        except Exception as e:
            erros.append({"nf": nota.get("nf_numero"), "erro": str(e)})
            continue
        if not xml:
            erros.append({"nf": nota.get("nf_numero"), "erro": "XML não retornado pelo Olist."})
            continue
        try:
            nf = nfe_parser.parse_nfe_bytes(xml.encode("utf-8"), f"olist-{chave}.xml")
        except Exception as e:
            erros.append({"nf": nota.get("nf_numero"), "erro": f"XML inválido: {e}"})
            continue
        m3 = None
        try:
            m3 = m3_da_nota(getattr(nf, "itens", None))
        except Exception:
            m3 = None
        res = romaneio_mod.atualizar_peso_volume(
            chave, peso_bruto=nf.peso_bruto, peso_liquido=nf.peso_liquido,
            volumes=nf.volumes, especie=nf.especie, m3=m3,
        )
        if res.get("atualizada"):
            atualizadas += 1
        else:
            # XML relido continua sem peso/volume — o dado não existe na origem.
            sem_dado += 1

    return {
        "ok": True,
        "pendentes": len(pendentes),
        "atualizadas": atualizadas,
        "sem_dado": sem_dado,
        "nao_encontradas": nao_encontradas,
        "erros": len(erros),
        "erros_detalhe": erros,
        "segundos": round(time.time() - inicio, 1),
    }


def status() -> Dict:
    estado = _carregar_estado()
    try:
        alertas = romaneio_mod.alertas_cancelamento()
    except Exception:
        alertas = []
    return {
        "configurado": configurado(),
        "auto": auto_ligado(),
        "intervalo_min": intervalo_min(),
        "janela_dias": janela_dias(),
        "janela_cancelamento_dias": janela_cancelamento_dias(),
        "ultima_sync": estado.get("ultima_sync"),
        "ultimo_resultado": estado.get("ultimo_resultado"),
        "alertas": alertas,
    }


# --------------------------------------------------------------------------- #
# Auto-sync em segundo plano
# --------------------------------------------------------------------------- #
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop() -> None:
    _stop.wait(20)  # deixa o servidor terminar de subir
    while not _stop.is_set():
        try:
            if configurado() and auto_ligado():
                res = sincronizar()
                if res.get("ok") and res.get("novas"):
                    logger.info(f"Olist auto-sync: {res['novas']} nota(s) nova(s) na fila do romaneio.")
                indice_custos()  # mantém o índice de custos das entradas quente (respeita o TTL)
        except Exception as e:
            logger.error(f"Olist auto-sync falhou: {e}")
        _stop.wait(intervalo_min() * 60)


def iniciar_auto() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    if not configurado():
        logger.info("Olist: token não configurado — auto-sync desligado. Preencha OLIST_TOKEN no .env.")
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="olist-sync", daemon=True)
    _thread.start()
    logger.info(f"Olist: auto-sync ligado (a cada {intervalo_min()} min, janela de {janela_dias()} dia(s)).")


def parar_auto() -> None:
    _stop.set()


# ─── Dimensões / cubagem por SKU (m³ por unidade) ────────────────────────────
_DIM_FILE = Path(os.getenv("OLIST_DIMENSOES", str(_ROOT / "tables" / "olist_dimensoes.json")))
_dim_cache: Optional[Dict[str, Optional[float]]] = None


def _load_dim() -> Dict[str, Optional[float]]:
    global _dim_cache
    if _dim_cache is None:
        try:
            _dim_cache = json.loads(_DIM_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _dim_cache = {}
    return _dim_cache


def _save_dim() -> None:
    try:
        _DIM_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DIM_FILE.write_text(json.dumps(_dim_cache or {}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def m3_por_sku(sku: str) -> Optional[float]:
    """m³ por unidade do produto (altura×largura×profundidade, cm→m³), via Olist,
    com cache em disco. None se não encontrar dimensões. Falha silenciosa (offline)."""
    sku = (sku or "").strip()
    if not sku:
        return None
    cache = _load_dim()
    if sku in cache:
        return cache[sku]
    m3: Optional[float] = None
    try:
        if configurado():
            achados = pesquisar_produtos(sku, limite=1)
            pid = achados[0]["id"] if achados else None
            if pid:
                p = _obter_produto(pid)
                a = _num(p.get("altura")) or 0
                l = _num(p.get("largura")) or 0
                c = _num(p.get("profundidade") or p.get("comprimento")) or 0
                if a > 0 and l > 0 and c > 0:
                    m3 = round(a * l * c / 1_000_000, 6)  # cm³ → m³
    except Exception:
        m3 = None
    cache[sku] = m3
    _save_dim()
    return m3


def m3_da_nota(itens: List[Dict]) -> Optional[float]:
    """Soma o m³ dos itens (SKU × quantidade). None se nenhum item tiver dimensão."""
    total = 0.0
    achou = False
    for it in itens or []:
        mu = m3_por_sku(it.get("codigo"))
        if mu:
            total += mu * float(it.get("quantidade") or 1)
            achou = True
    return round(total, 6) if achou else None


# --------------------------------------------------------------------------- #
# Conjuntos (Kits do Olist) — seletor de peso no comparativo do Cadastro
# --------------------------------------------------------------------------- #
_CONJ_FILE = Path(os.getenv("OLIST_CONJUNTOS", str(_ROOT / "tables" / "olist_conjuntos.json")))
_CONJ_TTL = int(os.getenv("OLIST_CONJUNTOS_TTL", str(6 * 3600)))  # 6h


def _listar_todos_produtos() -> List[Dict]:
    """Todas as páginas de produtos.pesquisa.php (sem filtro) — {id, nome, sku}."""
    out: List[Dict] = []
    pagina = 1
    while True:
        ret = json.loads(_post("produtos.pesquisa.php", {"pagina": str(pagina)})).get("retorno", {})
        if str(ret.get("status", "")).upper() != "OK":
            break
        for it in ret.get("produtos", []):
            p = it.get("produto", it)
            out.append({"id": str(p.get("id")), "nome": p.get("nome"), "sku": p.get("codigo")})
        if pagina >= int(ret.get("numero_paginas", pagina) or pagina):
            break
        pagina += 1
    return out


def listar_conjuntos(force: bool = False) -> List[Dict]:
    """Conjuntos (kits) cadastrados no Olist, para o seletor do comparativo.

    Detecta pelo prefixo de SKU 'CJ' (conjunto). Cacheia em disco por _CONJ_TTL
    para não varrer todas as páginas de produtos a cada abertura da tela.
    Retorna [{id, nome, sku}] ordenado por nome.
    """
    if not configurado():
        raise RuntimeError("Token do Olist não configurado. Defina OLIST_TOKEN no arquivo .env.")
    if not force:
        try:
            cache = json.loads(_CONJ_FILE.read_text(encoding="utf-8"))
            if time.time() - cache.get("ts", 0) < _CONJ_TTL and cache.get("itens"):
                return cache["itens"]
        except (OSError, json.JSONDecodeError):
            pass
    itens = [p for p in _listar_todos_produtos()
             if str(p.get("sku", "")).upper().startswith("CJ")]
    itens.sort(key=lambda p: (p.get("nome") or "").upper())
    try:
        _CONJ_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONJ_FILE.write_text(json.dumps({"ts": time.time(), "itens": itens}, ensure_ascii=False),
                              encoding="utf-8")
    except OSError:
        pass
    return itens


def peso_do_conjunto(pid: str, cubagem: float = 300.0) -> Dict:
    """Peso de um conjunto (kit) para o comparativo: o MAIOR entre o peso bruto e o
    peso cubado (m³ × fator de cubagem), igual ao romaneio. O m³ vem da soma dos
    componentes do kit (o kit não traz dimensão própria). Falha silenciosa: sem
    dimensão de componente, o cubado é ignorado e vale o peso bruto."""
    if not configurado():
        raise RuntimeError("Token do Olist não configurado. Defina OLIST_TOKEN no arquivo .env.")
    prod = _obter_produto(pid)
    if not prod:
        return {"ok": False, "erro": "Conjunto não encontrado no Olist."}
    peso_bruto = _num(prod.get("peso_bruto")) or 0.0
    m3 = 0.0
    achou_dim = False
    for c in (prod.get("kit") or []):
        item = c.get("item", c)
        cid = item.get("id_produto")
        if not cid:
            continue
        qtd = float(item.get("quantidade") or 1)
        comp = _obter_produto(str(cid))
        a = _num(comp.get("altura")) or 0
        l = _num(comp.get("largura")) or 0
        pr = _num(comp.get("profundidade") or comp.get("comprimento")) or 0
        if a > 0 and l > 0 and pr > 0:
            m3 += (a * l * pr / 1_000_000) * qtd
            achou_dim = True
    m3 = round(m3, 6)
    peso_cubado = round(m3 * cubagem, 2) if achou_dim else 0.0
    peso = round(max(peso_bruto, peso_cubado), 2)
    return {
        "ok": True,
        "id": str(pid),
        "nome": prod.get("nome"),
        "sku": prod.get("codigo"),
        "peso_bruto": round(peso_bruto, 2),
        "m3": m3 if achou_dim else None,
        "cubagem": cubagem,
        "peso_cubado": peso_cubado if achou_dim else None,
        "peso": peso,
    }
