"""
Motor de auditoria: compara o valor cobrado em um CTe com o valor que a
tabela de frete contratada calcularia para a mesma rota/peso/valor declarado.

Reaproveita rate_calculator.quote_for_carrier — a MESMA lógica de matching
(UF/REGIAO, faixa de peso, encargos) usada na cotação de frete, garantindo que
o "valor esperado" seja consistente com o que o gateway cotaria hoje para esse frete.
"""
from __future__ import annotations

import logging
from datetime import date
from enum import Enum
from typing import Dict, List, Optional, Tuple

import pandas as pd
from pydantic import BaseModel

from app import cadastro
from app.carrier_mapping import get_carrier, is_known_spot
from app.cep_resolver import resolve_cep
from app.cte_parser import CteParseError, parse_cte_bytes, parse_evento_cancelamento
from app.rate_calculator import _add_business_days, quote_for_carrier

logger = logging.getLogger(__name__)

TOLERANCIA_ABS_PADRAO = 1.0   # R$
TOLERANCIA_PCT_PADRAO = 2.0   # %

# Devolução/reentrega: a tabela contratada só tem a rota de ida — não tem como
# cotar essas operações contra ela. O combinado com as transportadoras é
# devolução = 100% do valor da ida e reentrega = 50% do valor da ida (mesma
# mercadoria, novo CT-e). O jeito mais confiável de identificar é o flag
# estruturado compl/xCaracAd ("DEVOLUCAO"/"REVERSA"/"REENTREGA" — nem toda
# transportadora preenche, mas quando preenche é autoritativo, melhor que
# procurar palavra-chave no texto livre de xObs). Sem o flag, cai no fallback
# heurístico (só pra devolução): DESTINO é o nosso hub E o CT-e transporta
# NF que NÓS emitimos — as duas juntas, pra não confundir com compra normal
# de insumo (chega no hub mas nunca refere NF nossa) nem com saída normal
# (refere NF nossa mas nunca tem o hub como destino).
NOSSO_CNPJ = "66957537000152"
NOSSA_CIDADE_HUB = ("SERRA", "ES")
_FLAGS_DEVOLUCAO = {"DEVOLUCAO", "DEVOLUÇÃO", "REVERSA"}
_FLAGS_REENTREGA = {"REENTREGA"}
_PCT_POR_TIPO = {"DEVOLUCAO": 1.0, "REENTREGA": 0.5}
_STATUS_SEM_REFERENCIA = {
    "DEVOLUCAO": "DEVOLUCAO_SEM_IDA",
    "REENTREGA": "REENTREGA_SEM_ORIGEM",
}
_ROTULO_TIPO = {"DEVOLUCAO": "DEVOLUÇÃO", "REENTREGA": "REENTREGA"}


class AuditStatus(str, Enum):
    OK = "OK"
    DIVERGENTE = "DIVERGENTE"
    SPOT = "SPOT"
    SEM_VALOR_COBRADO = "SEM_VALOR_COBRADO"
    TRANSPORTADORA_NAO_MAPEADA = "TRANSPORTADORA_NAO_MAPEADA"
    SEM_TABELA = "SEM_TABELA"
    SEM_COBERTURA = "SEM_COBERTURA"
    ERRO_LEITURA = "ERRO_LEITURA"
    DEVOLUCAO_SEM_IDA = "DEVOLUCAO_SEM_IDA"
    REENTREGA_SEM_ORIGEM = "REENTREGA_SEM_ORIGEM"
    CANCELADO = "CANCELADO"
    JA_LANCADO = "JA_LANCADO"


class AuditResult(BaseModel):
    arquivo: str
    status: AuditStatus
    motivo: str = ""

    chave_cte: Optional[str] = None
    numero_cte: Optional[str] = None
    data_emissao: Optional[str] = None

    transportadora_cnpj: Optional[str] = None
    transportadora_nome_cte: Optional[str] = None
    transportadora_tabela: Optional[str] = None

    origem: Optional[str] = None
    destino: Optional[str] = None
    peso_kg: Optional[float] = None
    valor_mercadoria: Optional[float] = None

    valor_cobrado: Optional[float] = None
    valor_tabela: Optional[float] = None
    diferenca: Optional[float] = None
    diferenca_pct: Optional[float] = None
    tipo_operacao: Optional[str] = None  # "DEVOLUCAO" | "REENTREGA" — sempre pendente p/ humano
    tp_cte: str = "0"          # ide/tpCTe: 0=Normal, 1=Complementar, 2=Anulação, 3=Substituto
    tp_serv: str = "0"         # ide/tpServ: 2/3 = redespacho
    cte_comp_chave: Optional[str] = None  # CT-e de origem que este complementa (tpCTe=1)
    cte_origem_chave: Optional[str] = None  # devolução/reentrega: chave do CT-e de IDA (origem)
    numero_nf: Optional[str] = None  # NF(s) transportada(s) — "123" ou "123, 456"
    destinatario_cnpj: Optional[str] = None  # CNPJ/CPF do cliente — usado p/ achar a ida de uma devolução

    charges_tabela: Optional[Dict[str, float]] = None
    componentes_cte: Optional[List[Dict]] = None

    # Torre de controle: prazo contratado e NF-es transportadas (elo com o OCOREN)
    prazo_dias: Optional[int] = None
    data_prevista: Optional[str] = None  # ISO date (emissão + prazo em dias úteis)
    notas: Optional[List[Dict]] = None


def peso_taxavel_cubado(peso_real: Optional[float], peso_base: Optional[float],
                        m3: Optional[float], cubagem_fator: float) -> Optional[float]:
    """Peso taxável verificando o cubado: max(peso físico, m³ × cubagem contratada).
    Se não há m³ ou cubagem, mantém o peso base do CTe (comportamento anterior).
    Mesma regra do romaneio — evita confiar cegamente no peso base da transportadora."""
    peso_cubado = (m3 * cubagem_fator) if (m3 and cubagem_fator) else None
    if peso_cubado is None:
        return peso_base
    peso_real = peso_real if peso_real else peso_base
    return max(peso_real, peso_cubado) if peso_real is not None else peso_cubado


def _within_tolerance(diff_abs: float, valor_tabela: float, tol_abs: float, tol_pct: float) -> bool:
    limite = max(tol_abs, valor_tabela * tol_pct / 100)
    return abs(diff_abs) <= limite


def _tipo_operacao_especial(cte) -> Optional[str]:
    """Retorna 'DEVOLUCAO', 'REENTREGA' ou None. Prioriza o flag estruturado
    (xCaracAd/xCaracSer); sem flag, cai no fallback heurístico só pra devolução
    (destino = nosso hub + transporta NF nossa) — reentrega sem flag não tem
    heurística segura o suficiente (o destino é normal, igual a qualquer ida)."""
    flag = (cte.caracteristica_adicional or "").strip().upper()
    if flag in _FLAGS_DEVOLUCAO:
        return "DEVOLUCAO"
    if flag in _FLAGS_REENTREGA:
        return "REENTREGA"

    destino_hub = (
        (cte.destino_municipio or "").strip().upper() == NOSSA_CIDADE_HUB[0]
        and (cte.destino_uf or "").strip().upper() == NOSSA_CIDADE_HUB[1]
    )
    # Origem TAMBÉM não pode ser o hub — senão uma entrega local normal (hub ->
    # cliente na mesma cidade do hub, ex.: RPS intramunicipal) bate a mesma
    # condição (destino=hub + carrega NF nossa, que TODA saída normal carrega)
    # e seria classificada como devolução por engano.
    origem_hub = (
        (cte.origem_municipio or "").strip().upper() == NOSSA_CIDADE_HUB[0]
        and (cte.origem_uf or "").strip().upper() == NOSSA_CIDADE_HUB[1]
    )
    if destino_hub and not origem_hub and any(n.cnpj_emitente == NOSSO_CNPJ for n in cte.notas):
        return "DEVOLUCAO"
    return None


def _audit_operacao_especial(cte, base: "AuditResult", tipo: str, tol_abs: float, tol_pct: float) -> "AuditResult":
    """Devolução/reentrega: a tabela contratada não tem como cotar essas
    operações (não existe "rota de volta"/"2ª tentativa" nela) — o combinado é
    uma % fixa do valor cobrado na IDA da mesma NF (100% devolução, 50%
    reentrega). Acha a ida já auditada (via app.tracking, que liga NF -> CT-e
    pela chave decomposta) e compara com a mesma tolerância do resto da
    auditoria."""
    from app import tracking

    base.transportadora_tabela = get_carrier(cte.transportadora_cnpj) or cte.transportadora_nome
    base.tipo_operacao = tipo
    rotulo = _ROTULO_TIPO[tipo]
    pct = _PCT_POR_TIPO[tipo]

    if not cte.valor_cobrado:
        base.status = AuditStatus.SEM_VALOR_COBRADO
        base.motivo = f"CT-e de {rotulo} sem valor da prestação (vTPrest ausente ou zerado)."
        return base

    ida = None
    for nota in cte.notas:
        if nota.cnpj_emitente != NOSSO_CNPJ:
            continue
        ida = tracking.buscar_valor_ida(nota.numero, nota.serie, nota.cnpj_emitente,
                                        excluir_chave_cte=cte.chave)
        if ida:
            break

    # Fallback: NF não bateu (ida nunca foi auditada com essa NF, ou a
    # devolução não carrega chave de NF-e) — tenta pelo CNPJ/CPF do cliente.
    # Devolução: quem manda de volta é quem recebeu a ida (remetente da volta
    # = destinatário da ida). Reentrega: mesmo sentido da ida, é o destinatário.
    por_cliente = False
    if not ida:
        cliente_cnpj = cte.remetente_cnpj if tipo == "DEVOLUCAO" else cte.destinatario_cnpj
        if cliente_cnpj:
            ida = tracking.buscar_valor_ida_por_cliente(
                cliente_cnpj, antes_de=cte.data_emissao, excluir_chave_cte=cte.chave)
            por_cliente = ida is not None

    # Persiste a origem encontrada (mesmo sem valor) para a Torre vincular a
    # devolução/reentrega ao CT-e de ida automaticamente (ver tracking.upsert).
    if ida and ida.get("chave_cte"):
        base.cte_origem_chave = ida["chave_cte"]

    if not ida or not ida.get("valor_cobrado"):
        base.status = AuditStatus(_STATUS_SEM_REFERENCIA[tipo])
        base.motivo = (
            f"CT-e de {rotulo}, mas o CT-e de ida correspondente (pela NF ou pelo cliente) "
            "ainda não foi encontrado na auditoria — confira manualmente."
        )
        return base

    esperado = round(ida["valor_cobrado"] * pct, 2)
    base.valor_tabela = esperado
    diff = round(cte.valor_cobrado - esperado, 2)
    base.diferenca = diff
    base.diferenca_pct = round((diff / esperado) * 100, 2) if esperado else None

    ref = f"{int(pct * 100)}% do CT-e de ida nº {ida.get('numero_cte') or '?'} (R$ {ida['valor_cobrado']:.2f} → R$ {esperado:.2f})"
    if por_cliente:
        ref += " — achado pelo CPF/CNPJ do cliente, não pela NF; confirme se é a mercadoria certa"
    if _within_tolerance(diff, esperado, tol_abs, tol_pct):
        base.status = AuditStatus.OK
        base.motivo = f"CT-e de {rotulo} — dentro da tolerância de {ref}."
    else:
        base.status = AuditStatus.DIVERGENTE
        sinal = "a mais" if diff > 0 else "a menos"
        base.motivo = f"CT-e de {rotulo} — cobrado R$ {abs(diff):.2f} {sinal} do que {ref}."
    return base


def vincular_ida_manual(
    chave_cte: str, numero_nf: str, usuario: str = "",
    tol_abs: float = TOLERANCIA_ABS_PADRAO, tol_pct: float = TOLERANCIA_PCT_PADRAO,
) -> Dict:
    """Vínculo manual pra devolução/reentrega SEM_IDA/SEM_ORIGEM: o usuário
    digita o nº da NF de origem quando a busca automática (por NF exata ou
    por CPF/CNPJ do cliente) não achou nada — geralmente porque a ida ainda
    não tinha sido auditada na hora, ou a devolução não carregava a chave da
    NF-e no XML. Refaz a mesma conta de app._audit_operacao_especial (100%
    devolução / 50% reentrega) usando o valor já salvo no histórico, sem
    precisar reabrir o XML original."""
    from app import history as audit_history, tracking

    atual = audit_history.get_by_chave(chave_cte)
    if atual is None:
        raise ValueError("CT-e não encontrado no histórico.")
    tipo = atual.get("tipo_operacao")
    if tipo not in _PCT_POR_TIPO:
        raise ValueError("Este CT-e não é uma devolução/reentrega — o vínculo manual só se aplica a esses casos.")
    if not atual.get("valor_cobrado"):
        raise ValueError("CT-e sem valor cobrado — não é possível conferir.")

    ida = tracking.buscar_valor_ida_manual(numero_nf, excluir_chave_cte=chave_cte)
    if not ida or not ida.get("valor_cobrado"):
        raise ValueError(f"Nenhum CT-e de ida já auditado encontrado para a NF {numero_nf}.")

    pct = _PCT_POR_TIPO[tipo]
    rotulo = _ROTULO_TIPO[tipo]
    esperado = round(ida["valor_cobrado"] * pct, 2)
    diff = round(atual["valor_cobrado"] - esperado, 2)
    diff_pct = round((diff / esperado) * 100, 2) if esperado else None

    ref = f"{int(pct * 100)}% do CT-e de ida nº {ida.get('numero_cte') or '?'} (R$ {ida['valor_cobrado']:.2f} → R$ {esperado:.2f})"
    quem = f" por {usuario}" if usuario else ""
    if _within_tolerance(diff, esperado, tol_abs, tol_pct):
        status = AuditStatus.OK
        motivo = f"CT-e de {rotulo} — vinculado manualmente à NF {numero_nf}{quem}, dentro da tolerância de {ref}."
    else:
        status = AuditStatus.DIVERGENTE
        sinal = "a mais" if diff > 0 else "a menos"
        motivo = f"CT-e de {rotulo} — vinculado manualmente à NF {numero_nf}{quem}, cobrado R$ {abs(diff):.2f} {sinal} do que {ref}."

    atualizado = audit_history.atualizar_vinculo_manual(
        chave_cte, status=status.value, motivo=motivo,
        valor_tabela=esperado, diferenca=diff, diferenca_pct=diff_pct,
    )
    if atualizado is None:
        raise ValueError("Falha ao gravar o vínculo — CT-e não encontrado.")

    # Também vincula na Torre de Controle (mesma UX de lá) quando dá — falha
    # silenciosa: o vínculo da auditoria (o que importa pro financeiro) já
    # foi gravado acima independente disso.
    if tipo == "DEVOLUCAO":
        try:
            tracking.vincular_devolucao(chave_cte, ida["chave_cte"])
        except Exception:
            logger.exception(f"Falha ao vincular devolução na Torre de Controle: {chave_cte} -> {ida['chave_cte']}")

    return atualizado


def _fatura_do_cte(chave_cte: str) -> Optional[Dict]:
    from app import conferencia
    return conferencia.fatura_de_cte(chave_cte)


def _ja_auditado(chave_cte: str) -> bool:
    from app import history as audit_history
    existente = audit_history.get_by_chave(chave_cte)
    return existente is not None and existente.get("valor_cobrado") is not None


def _audit_cancelamento(evento, filename: str) -> "AuditResult":
    """Evento de cancelamento (SEFAZ): não é um CT-e novo, é a baixa de um CT-e
    já auditado. app.history.save_result trata status=CANCELADO como update
    mínimo (preserva os dados originais do CT-e cancelado)."""
    return AuditResult(
        arquivo=filename,
        status=AuditStatus.CANCELADO,
        chave_cte=evento.chave,
        motivo=(
            f"CT-e cancelado pela transportadora"
            + (f" — motivo: {evento.justificativa}" if evento.justificativa else "")
            + ". Confira se foi reemitido (novo nº de CT-e) pra não contar como duplicado."
        ),
    )


async def audit_cte_bytes(
    data: bytes,
    filename: str,
    precos: Dict[str, Dict],
    prazos: Dict[str, pd.DataFrame],
    tol_abs: float = TOLERANCIA_ABS_PADRAO,
    tol_pct: float = TOLERANCIA_PCT_PADRAO,
) -> AuditResult:
    if filename.lower().endswith(".pdf"):
        # RPS/NFS-e (serviço intramunicipal, sem CT-e — ver app.rps_parser):
        # devolve o mesmo CteData do XML, então dali pra baixo é tudo igual.
        from app.rps_parser import RpsParseError, parse_rps_bytes
        try:
            cte = parse_rps_bytes(data, filename=filename)
        except RpsParseError as e:
            return AuditResult(arquivo=filename, status=AuditStatus.ERRO_LEITURA, motivo=str(e))
        except Exception as e:
            logger.exception(f"Erro inesperado lendo RPS {filename}")
            return AuditResult(arquivo=filename, status=AuditStatus.ERRO_LEITURA, motivo=f"Erro inesperado: {e}")
    else:
        evento = parse_evento_cancelamento(data)
        if evento is not None:
            return _audit_cancelamento(evento, filename)

        try:
            cte = parse_cte_bytes(data, filename=filename)
        except CteParseError as e:
            return AuditResult(arquivo=filename, status=AuditStatus.ERRO_LEITURA, motivo=str(e))
        except Exception as e:
            logger.exception(f"Erro inesperado lendo {filename}")
            return AuditResult(arquivo=filename, status=AuditStatus.ERRO_LEITURA, motivo=f"Erro inesperado: {e}")

    numeros_nf = list(dict.fromkeys(n.numero for n in cte.notas if n.numero))

    base = AuditResult(
        arquivo=filename,
        status=AuditStatus.ERRO_LEITURA,
        chave_cte=cte.chave,
        numero_cte=cte.numero,
        data_emissao=cte.data_emissao,
        transportadora_cnpj=cte.transportadora_cnpj,
        transportadora_nome_cte=cte.transportadora_nome,
        origem=f"{cte.origem_municipio}/{cte.origem_uf}" if cte.origem_municipio else None,
        destino=f"{cte.destino_municipio}/{cte.destino_uf}" if cte.destino_municipio else None,
        peso_kg=cte.peso_kg,
        valor_mercadoria=cte.valor_mercadoria,
        valor_cobrado=cte.valor_cobrado,
        componentes_cte=[{"nome": c.nome, "valor": c.valor} for c in cte.componentes],
        notas=[
            {"chave": n.chave, "cnpj_emitente": n.cnpj_emitente, "serie": n.serie, "numero": n.numero}
            for n in cte.notas
        ],
        numero_nf=", ".join(numeros_nf) if numeros_nf else None,
        destinatario_cnpj=cte.destinatario_cnpj or None,
        tp_cte=getattr(cte, "tp_cte", "0") or "0",
        tp_serv=getattr(cte, "tp_serv", "0") or "0",
        cte_comp_chave=getattr(cte, "cte_comp_chave", "") or None,
    )

    # CT-e já AUDITADO antes e já lançado numa fatura: trava a REimportação.
    # Reprocessar o XML reauditaria (e poderia repreçar) um documento que já
    # está numa fatura — mesmo "aberta" — arriscando destoar do que já foi
    # conciliado/pago. Só bloqueia se já existe auditoria prévia: uma fatura
    # pode citar um CT-e ainda não auditado (status NAO_AUDITADO na
    # conferência) — nesse caso a primeira importação tem que passar, é ela
    # que resolve o NAO_AUDITADO sozinho (ver app.conferencia).
    ja_auditado = _ja_auditado(cte.chave)
    if ja_auditado:
        lancamento = _fatura_do_cte(cte.chave)
        if lancamento:
            base.status = AuditStatus.JA_LANCADO
            base.motivo = (
                f"CT-e já lançado na fatura nº {lancamento.get('numero_fatura') or lancamento['id']} "
                f"({lancamento.get('transportadora') or '—'}, status '{lancamento.get('status') or '—'}') — "
                "reimportação bloqueada. Se precisa corrigir, ajuste a fatura ou remova o CT-e dela primeiro."
            )
            return base

    # Devolução/reentrega tem prioridade sobre SPOT: o CTe muitas vezes também
    # carrega "TABELA: INFORMADO" (preço batido manualmente na emissão), mas o
    # tipo de operação é o que manda na regra de cobrança (100%/50% da ida),
    # não o jeito como a transportadora registrou o preço na emissão dela.
    tipo_especial = _tipo_operacao_especial(cte)
    if tipo_especial:
        return _audit_operacao_especial(cte, base, tipo_especial, tol_abs, tol_pct)

    if cte.is_spot_declarado:
        base.status = AuditStatus.SPOT
        base.motivo = "CTe indica frete negociado avulso (\"TABELA: INFORMADO\"), fora da tabela contratada."
        return base

    if is_known_spot(cte.transportadora_cnpj):
        base.status = AuditStatus.SPOT
        base.motivo = "Transportadora cadastrada como frete spot (sem tabela contratada)."
        return base

    carrier_name = get_carrier(cte.transportadora_cnpj)
    if carrier_name is None:
        base.status = AuditStatus.TRANSPORTADORA_NAO_MAPEADA
        base.motivo = (
            f"CNPJ {cte.transportadora_cnpj} ({cte.transportadora_nome}) ainda não está "
            "mapeado para nenhuma aba de tabela. Cadastre o mapeamento ou marque como spot."
        )
        return base

    base.transportadora_tabela = carrier_name

    # Metas de serviço por transportadora (base de acordo) sobrepõem a tolerância
    # global quando a transportadora está cadastrada no painel /cadastro.
    tol_transp = cadastro.tolerancia_por_transportadora(carrier_name)
    if tol_transp:
        tol_abs, tol_pct = tol_transp["tol_abs"], tol_transp["tol_pct"]

    precos_data = precos.get(carrier_name)
    prazos_df = prazos.get(carrier_name)
    if precos_data is None or prazos_df is None:
        base.status = AuditStatus.SEM_TABELA
        base.motivo = f"Transportadora '{carrier_name}' mapeada, mas sem tabela de preço/prazo carregada."
        return base

    # Vigência de tabela: se a emissão do CTe é anterior à vigência da tabela atual,
    # audita contra a tabela que valia na época (evita divergência falsa após um
    # reajuste). Retorna None quando a tabela atual se aplica — aí mantém o overlay.
    usou_vigencia = False
    if cte.data_emissao:
        try:
            hist = cadastro.tabela_vigente_em_por_nome(carrier_name, cte.data_emissao[:10])
            if hist:
                precos_data, prazos_df = hist
                usou_vigencia = True
        except Exception:
            pass  # qualquer falha na vigência cai na tabela atual, sem quebrar a auditoria

    if not cte.destino_cep:
        base.status = AuditStatus.ERRO_LEITURA
        base.motivo = "Não foi possível extrair o CEP de destino do CTe."
        return base

    if cte.peso_kg is None:
        base.status = AuditStatus.ERRO_LEITURA
        base.motivo = "Não foi possível extrair o peso taxado do CTe."
        return base

    # Sem valor cobrado (vTPrest ausente/zero) não dá para auditar divergência —
    # comparar contra R$0,00 geraria um falso DIVERGENTE. Sinaliza para conferência.
    if not cte.valor_cobrado:
        base.status = AuditStatus.SEM_VALOR_COBRADO
        base.motivo = "CTe sem valor da prestação (vTPrest ausente ou zerado) — não é possível auditar a cobrança."
        return base

    city_uf: Optional[Tuple[str, str]] = None
    if cte.destino_municipio and cte.destino_uf:
        city_uf = (cte.destino_municipio.upper(), cte.destino_uf.upper())
    else:
        city_uf = await resolve_cep(cte.destino_cep)

    valor_nao_encontrado = cte.valor_mercadoria is None
    declared_value = cte.valor_mercadoria or 0.0

    # Peso taxável: verifica o peso cubado de forma independente — max entre o peso
    # físico e (m³ × cubagem contratada) — em vez de confiar cegamente no "peso base
    # de cálculo" do CTe (que a transportadora pode ter inflado). Mesma regra do
    # romaneio. Só muda quando há m³ no CTe E cubagem_fator no acordo; senão mantém
    # o peso do CTe (comportamento anterior, sem falso positivo).
    cubagem_fator = float(precos_data.get("meta", {}).get("cubagem_fator", 0) or 0)
    peso_real = cte.peso_real if cte.peso_real else cte.peso_kg
    peso_cubado = (cte.m3 * cubagem_fator) if (cte.m3 and cubagem_fator) else None
    peso_taxavel = peso_taxavel_cubado(cte.peso_real, cte.peso_kg, cte.m3, cubagem_fator)

    dm, reason = quote_for_carrier(
        carrier_name,
        precos_data,
        prazos_df,
        cte.destino_cep,
        peso_taxavel,
        declared_value,
        city_uf,
        date.today(),
        cnpj_destinatario=cte.destinatario_cnpj,
    )

    if dm is None:
        base.status = AuditStatus.SEM_COBERTURA
        base.motivo = reason
        return base

    base.valor_tabela = dm.final_shipping_cost
    base.charges_tabela = dm.charges
    base.prazo_dias = dm.delivery_estimate_business_days
    if base.prazo_dias and cte.data_emissao:
        try:
            emissao = date.fromisoformat(cte.data_emissao[:10])
            base.data_prevista = _add_business_days(emissao, base.prazo_dias).isoformat()
        except ValueError:
            pass
    diff = round(cte.valor_cobrado - dm.final_shipping_cost, 2)
    base.diferenca = diff
    base.diferenca_pct = round((diff / dm.final_shipping_cost) * 100, 2) if dm.final_shipping_cost else None

    if _within_tolerance(diff, dm.final_shipping_cost, tol_abs, tol_pct):
        base.status = AuditStatus.OK
        base.motivo = "Valor cobrado dentro da tolerância em relação à tabela."
    else:
        base.status = AuditStatus.DIVERGENTE
        sinal = "a mais" if diff > 0 else "a menos"
        base.motivo = f"Cobrado R$ {abs(diff):.2f} {sinal} do que a tabela prevê."

    if usou_vigencia:
        base.motivo += " ℹ Auditado contra a tabela vigente na data de emissão (vigência arquivada)."

    # Cubagem inflada: a transportadora cobrou sobre um peso base maior do que o
    # peso taxável esperado (real vs m³ × cubagem contratada). Sinaliza a causa —
    # parte da diferença pode ser peso cubado a mais, não tabela errada.
    if peso_cubado and cte.peso_kg and cte.peso_kg > peso_taxavel * 1.02:
        base.motivo += (
            f" ⚠ Peso cubado: base cobrada {cte.peso_kg:.0f} kg acima do esperado "
            f"{peso_taxavel:.0f} kg (real {peso_real:.0f} kg / {cte.m3:.2f} m³ × "
            f"{cubagem_fator:.0f}). Parte da diferença pode ser cubagem inflada."
        )

    if cte.serie == "RPS":
        base.motivo += (
            " ⚠ Documento é um RPS/NFS-e (serviço intramunicipal) — a tabela aplica ICMS "
            "(alíquota \"por dentro\"), mas o RPS cobra ISS municipal (alíquota simples, "
            "somada por fora); parte da diferença pode ser só esse descasamento de imposto, "
            "não necessariamente cobrança errada. Confira o ISS do RPS antes de contestar."
        )

    if valor_nao_encontrado:
        base.motivo += (
            " ⚠ Valor da mercadoria não foi encontrado no CTe (assumido R$0,00) — "
            "GRIS/ad valorem da tabela podem estar subestimados; confira manualmente."
        )

    return base


async def audit_many(
    files: List[Tuple[str, bytes]],
    precos: Dict[str, Dict],
    prazos: Dict[str, pd.DataFrame],
    tol_abs: float = TOLERANCIA_ABS_PADRAO,
    tol_pct: float = TOLERANCIA_PCT_PADRAO,
) -> List[AuditResult]:
    results = []
    for filename, data in files:
        results.append(await audit_cte_bytes(data, filename, precos, prazos, tol_abs, tol_pct))
    return results
