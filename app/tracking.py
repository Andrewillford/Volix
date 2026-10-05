"""
Torre de Controle de Entregas: cruza o prazo contratado (CTe + tabela de prazos)
com as ocorrências reais enviadas pelas transportadoras via EDI OCOREN.

Fluxo:
 1. Toda auditoria de CTe registra/atualiza uma "entrega" (data prevista =
    emissão + prazo em dias úteis) e as NF-es transportadas.
 2. A importação de OCOREN insere ocorrências e as vincula à entrega pela
    tripla (CNPJ emissor da NF, série, número) — a mesma decomposta da chave
    de acesso da NF-e presente no XML do CTe.
 3. Ocorrência de categoria ENTREGUE baixa a NF; quando todas as NFs do CTe
    estão baixadas, a entrega ganha data real e entra no OTD.

Ocorrência que chega antes do CTe ser auditado NÃO se perde: fica sem vínculo
(chave_cte NULL) e é religada automaticamente na próxima auditoria.

Usa o mesmo SQLite do histórico de auditorias (tabelas próprias).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

from app.history import DB_PATH
from app.feriados import somar_dias_uteis
from app.ocoren_parser import OcorenParseResult, OcorrenciaEdi

logger = logging.getLogger(__name__)

CODIGOS_FILE = os.getenv(
    "OCOREN_CODIGOS_FILE",
    str(Path(__file__).parent.parent / "tables" / "ocorrencias_codigos.json"),
)
CULPA_FILE = os.getenv(
    "TENTATIVAS_CULPA_FILE",
    str(Path(__file__).parent.parent / "tables" / "tentativas_culpa.json"),
)

CATEGORIAS = {"ENTREGUE", "EM_TRANSITO", "TENTATIVA_FRUSTRADA", "DEVOLUCAO", "SINISTRO", "OUTROS"}

_codigos_cache: Optional[Dict[str, Dict[str, str]]] = None


def load_codigos(force: bool = False) -> Dict[str, Dict[str, str]]:
    """Tabela código -> {descricao, categoria}. Arquivo editável pelo usuário."""
    global _codigos_cache
    if _codigos_cache is not None and not force:
        return _codigos_cache
    codigos: Dict[str, Dict[str, str]] = {}
    try:
        raw = json.loads(Path(CODIGOS_FILE).read_text(encoding="utf-8"))
        for cod, info in (raw.get("codigos") or {}).items():
            cat = str(info.get("categoria", "OUTROS")).upper()
            codigos[str(cod).zfill(2)] = {
                "descricao": str(info.get("descricao", "")),
                "categoria": cat if cat in CATEGORIAS else "OUTROS",
            }
    except FileNotFoundError:
        logger.warning(f"Arquivo de códigos de ocorrência não encontrado: {CODIGOS_FILE}")
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Falha lendo {CODIGOS_FILE}: {e}")
    _codigos_cache = codigos
    return codigos


def _normalizar_codigo(codigo: str) -> str:
    """'001' -> '01' (casa com a tabela). Códigos não numéricos (ex.: 'SSW-01')
    passam sem alteração relevante."""
    c = (codigo or "").strip().lstrip("0")
    return (c or "0").zfill(2)


def _norm_serie(s) -> str:
    """Normaliza a série da NF para comparação: tira zeros à esquerda para que
    '01' (formato do webhook Jamef) e '1' (XML da NF-e) sejam a mesma série.
    '', '0', '00' -> '0'."""
    return (str(s or "").strip().lstrip("0")) or "0"


_culpa_cache: Optional[Dict[str, set]] = None


def load_culpa(force: bool = False) -> Dict[str, set]:
    """Classificação dos códigos de TENTATIVA_FRUSTRADA por culpa (arquivo editável
    tables/tentativas_culpa.json). {'cliente': set, 'transportadora': set} — códigos
    normalizados. Base do OTD ajustado: tentativa no prazo por culpa do cliente
    (ausente/fechado/recusa) perdoa a transportadora; culpa dela não perdoa."""
    global _culpa_cache
    if _culpa_cache is not None and not force:
        return _culpa_cache
    cliente: set = set()
    transp: set = set()
    try:
        raw = json.loads(Path(CULPA_FILE).read_text(encoding="utf-8"))
        cliente = {_normalizar_codigo(c) for c in (raw.get("cliente") or [])}
        transp = {_normalizar_codigo(c) for c in (raw.get("transportadora") or [])}
    except FileNotFoundError:
        logger.warning(f"Arquivo de culpa de tentativas não encontrado: {CULPA_FILE}")
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Falha lendo {CULPA_FILE}: {e}")
    _culpa_cache = {"cliente": cliente, "transportadora": transp}
    return _culpa_cache


def _sem_acento(s: str) -> str:
    """minúsculas e sem acento, para comparar descrições de ocorrência."""
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)
    ).lower()


# Descrições cuja ocorrência significa "saiu para entrega / em rota de entrega".
# Casa por texto (não por código) para incluir automaticamente qualquer código que
# você mapear com essa redação na tabela de códigos (hoje: SSW-00, SSW-85).
_ROTA_ENTREGA_MARCADORES = ("saida para entrega", "em rota de entrega", "saiu para entrega")


def _eh_rota_entrega(descricao: str) -> bool:
    d = _sem_acento(descricao)
    return any(m in d for m in _ROTA_ENTREGA_MARCADORES)


def _classificar(codigo: str) -> Dict[str, str]:
    codigos = load_codigos()
    # Tenta o código como veio (ex.: 'SSW-01') e, se não achar, a forma normalizada
    # ('001' -> '01') — assim um código não normalizado na origem ainda casa com a
    # tabela e recebe a categoria certa (ex.: 01 = Entrega realizada = ENTREGUE).
    info = codigos.get(codigo) or codigos.get(_normalizar_codigo(codigo))
    if info:
        return info
    return {"descricao": f"Código {codigo} (não mapeado — edite ocorrencias_codigos.json)", "categoria": "OUTROS"}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entregas (
            chave_cte TEXT PRIMARY KEY,
            numero_cte TEXT,
            arquivo TEXT,
            transportadora TEXT,
            transportadora_cnpj TEXT,
            destino TEXT,
            data_emissao TEXT,
            prazo_dias INTEGER,
            data_prevista TEXT,
            data_entrega_real TEXT,
            entrega_manual INTEGER DEFAULT 0,
            devolvida INTEGER DEFAULT 0,
            criado_em TEXT,
            atualizado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS entrega_nfs (
            chave_cte TEXT,
            chave_nfe TEXT,
            cnpj_emissor TEXT,
            serie TEXT,
            numero_nf TEXT,
            data_entrega TEXT,
            PRIMARY KEY (chave_cte, cnpj_emissor, serie, numero_nf)
        );
        CREATE INDEX IF NOT EXISTS idx_entrega_nfs_nf ON entrega_nfs (cnpj_emissor, numero_nf);
        CREATE TABLE IF NOT EXISTS ocorrencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cnpj_emissor TEXT,
            serie TEXT,
            numero_nf TEXT,
            chave_cte TEXT,
            codigo TEXT,
            descricao TEXT,
            categoria TEXT,
            data_ocorrencia TEXT,
            hora TEXT,
            codigo_observacao TEXT,
            texto TEXT,
            transportadora_cnpj TEXT,
            arquivo_origem TEXT,
            importado_em TEXT,
            UNIQUE (cnpj_emissor, serie, numero_nf, codigo, data_ocorrencia, hora)
        );
        CREATE INDEX IF NOT EXISTS idx_ocorrencias_cte ON ocorrencias (chave_cte);
        CREATE INDEX IF NOT EXISTS idx_ocorrencias_nf ON ocorrencias (cnpj_emissor, numero_nf);
        -- Ocorrências recebidas antes de a NF ter romaneio liberado. Ficam em espera
        -- (não descartadas) e são reprocessadas quando o romaneio é liberado, para
        -- que o rastreio dessas entregas não se perca.
        CREATE TABLE IF NOT EXISTS ocorrencias_pendentes (
            cnpj_emissor TEXT,
            serie TEXT,
            numero_nf TEXT,
            codigo TEXT,
            data_ocorrencia TEXT,
            hora TEXT,
            codigo_observacao TEXT,
            texto TEXT,
            transportadora_cnpj TEXT,
            arquivo_origem TEXT,
            guardado_em TEXT,
            UNIQUE (cnpj_emissor, serie, numero_nf, codigo, data_ocorrencia, hora)
        );
        CREATE INDEX IF NOT EXISTS idx_ocorr_pend_nf ON ocorrencias_pendentes (cnpj_emissor, numero_nf);
    """)
    # Migração: data de LIBERAÇÃO do romaneio (quando a carga foi liberada para a
    # transportadora) — base do prazo de entrega e do que a Torre mostra na coluna
    # "Liberação". Preserva data_emissao (emissão do CTe). Renomeia a coluna antiga
    # data_expedicao (data de criação do romaneio), que não é a base correta.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entregas)").fetchall()}
    if "data_liberacao" not in cols:
        if "data_expedicao" in cols:
            conn.execute("ALTER TABLE entregas RENAME COLUMN data_expedicao TO data_liberacao")
        else:
            conn.execute("ALTER TABLE entregas ADD COLUMN data_liberacao TEXT")
    # Devolução: o CT-e de retorno guarda aqui a chave do CT-e ORIGINAL que ele
    # devolve. Marca a entrega como "devolução" (rótulo próprio, fora do OTD); o
    # original é marcado devolvida=1. Ver vincular_devolucao().
    if "devolucao_de" not in cols:
        conn.execute("ALTER TABLE entregas ADD COLUMN devolucao_de TEXT")
    # Unificação: CT-e secundário (reentrega/complementar/redespacho) da MESMA
    # entrega. `unificado_em` guarda a chave do CT-e de ORIGEM; quando setado, some
    # da Torre como linha separada (rola para dentro da origem). `tipo_cte` guarda o
    # motivo (REENTREGA/COMPLEMENTAR/REDESPACHO) para exibir no marcador.
    if "unificado_em" not in cols:
        conn.execute("ALTER TABLE entregas ADD COLUMN unificado_em TEXT")
    if "tipo_cte" not in cols:
        conn.execute("ALTER TABLE entregas ADD COLUMN tipo_cte TEXT")
    conn.commit()
    conn.close()


# ─── Agrupamento de transportadora por CNPJ (grupo/logística reversa) ─────────

def _nome_transportadora_grupo(cnpj: str, nome_cru: Optional[str],
                               mapping: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Nome CANÔNICO da transportadora para agrupar na Torre. Resolve o CNPJ pelo
    grupo cadastrado (mapa CNPJ→nome): match EXATO e, na falta, pela RAIZ do CNPJ
    (8 primeiros dígitos = mesma empresa, filial/logística reversa diferente). Assim
    todas as filiais de uma transportadora caem sob o mesmo nome sem duplicar. Cai
    no nome do CTe quando não há grupo (CNPJ desconhecido ou SPOT explícito)."""
    from app import carrier_mapping as _cm
    cc = re.sub(r"\D", "", cnpj or "")
    if not cc:
        return nome_cru
    if mapping is None:
        mapping = _cm.load_mapping()
    val = mapping.get(cc)
    if val and val != _cm.SPOT_SENTINEL:
        return val
    if val == _cm.SPOT_SENTINEL:
        return nome_cru  # SPOT explícito: não agrupa
    raiz = cc[:8]
    nomes = {v for k, v in mapping.items()
             if v and v != _cm.SPOT_SENTINEL and re.sub(r"\D", "", k)[:8] == raiz}
    return next(iter(nomes)) if len(nomes) == 1 else nome_cru


# ─── Unificação de CT-e secundário no CT-e de origem ──────────────────────────

def _tipo_cte_unificavel(result) -> Optional[str]:
    """Classifica o CT-e como SECUNDÁRIO de uma entrega já representada por outro
    CT-e (a 'origem'): COMPLEMENTAR (tpCTe=1), REDESPACHO (tpServ 2/3) ou REENTREGA
    (xCaracAd). Devolução NÃO entra aqui — segue como linha própria. None = normal."""
    if str(getattr(result, "tp_cte", "0") or "0") == "1":
        return "COMPLEMENTAR"
    if str(getattr(result, "tp_serv", "0") or "0") in ("2", "3"):
        return "REDESPACHO"
    if (getattr(result, "tipo_operacao", "") or "") == "REENTREGA":
        return "REENTREGA"
    return None


def _achar_origem_unificacao(conn, notas, chave_self, cte_comp_chave, tipo) -> Optional[str]:
    """Chave do CT-e de ORIGEM em que este secundário deve ser unificado. Complementar
    aponta direto para o CT-e complementado (infCteComp); reentrega/redespacho acham a
    entrega mais antiga que cobre a mesma NF e NÃO é secundária nem devolução."""
    if tipo == "COMPLEMENTAR" and cte_comp_chave:
        row = conn.execute("SELECT chave_cte FROM entregas WHERE chave_cte = ?", (cte_comp_chave,)).fetchone()
        if row:
            return cte_comp_chave
    for nf in (notas or []):
        cnpj = re.sub(r"\D", "", nf.get("cnpj_emitente") or nf.get("cnpj_emissor") or "")
        numero = str(nf.get("numero") or nf.get("numero_nf") or "").lstrip("0")
        if not (cnpj and numero):
            continue
        row = conn.execute("""
            SELECT e.chave_cte FROM entrega_nfs n JOIN entregas e ON e.chave_cte = n.chave_cte
            WHERE n.cnpj_emissor = ? AND n.numero_nf = ? AND e.chave_cte <> ?
              AND e.chave_cte NOT LIKE 'ROM:%'
              AND e.unificado_em IS NULL AND e.devolucao_de IS NULL AND e.tipo_cte IS NULL
            ORDER BY e.data_emissao LIMIT 1
        """, (cnpj, numero, chave_self or "")).fetchone()
        if row:
            return row["chave_cte"]
    return None


# ─── Registro de entregas a partir da auditoria ──────────────────────────────

def upsert_from_audit(result) -> None:
    """result: app.auditor.AuditResult. Cria/atualiza a entrega e suas NFs,
    religa ocorrências pendentes e recalcula a baixa da entrega."""
    if not result.chave_cte:
        return
    now = datetime.now().isoformat(timespec="seconds")
    emissao = (result.data_emissao or "")[:10] or None

    # O prazo de entrega conta a partir da LIBERAÇÃO DO ROMANEIO (quando a carga é
    # liberada para a transportadora), não da emissão da NF/CTe nem da criação do
    # romaneio. O auditor calcula `data_prevista` sobre a emissão do CTe (não
    # conhece romaneio); aqui gravamos a liberação em `data_liberacao` e
    # recalculamos a previsão sobre ela. Sem romaneio liberado para as NFs,
    # `data_liberacao` fica nula e tudo cai para a emissão do CTe.
    data_liberacao = _liberacao_das_nfs(result.notas)
    base = data_liberacao or emissao
    data_prevista = result.data_prevista
    if result.prazo_dias and base:
        try:
            data_prevista = somar_dias_uteis(
                date.fromisoformat(base), int(result.prazo_dias)
            ).isoformat()
        except (ValueError, TypeError):
            pass

    conn = _get_conn()
    try:
        existing = conn.execute(
            "SELECT criado_em, data_entrega_real, entrega_manual, devolvida FROM entregas WHERE chave_cte = ?",
            (result.chave_cte,),
        ).fetchone()

        # Classificação do CT-e. DEVOLUÇÃO: marca como tal (rótulo próprio na Torre,
        # nunca se perde) e auto-vincula ao CT-e de ida que o auditor encontrou.
        # Reentrega/complementar/redespacho: unifica no CT-e de origem.
        is_devolucao = (getattr(result, "tipo_operacao", "") or "") == "DEVOLUCAO"
        devolucao_origem = None
        if is_devolucao:
            tipo_cte = "DEVOLUCAO"
            unificado_em = None
            oc = getattr(result, "cte_origem_chave", None)
            if oc and conn.execute("SELECT 1 FROM entregas WHERE chave_cte = ?", (oc,)).fetchone():
                devolucao_origem = oc
        else:
            tipo_cte = _tipo_cte_unificavel(result)
            unificado_em = (_achar_origem_unificacao(
                conn, result.notas, result.chave_cte,
                getattr(result, "cte_comp_chave", None), tipo_cte) if tipo_cte else None)

        conn.execute("""
            INSERT INTO entregas (
                chave_cte, numero_cte, arquivo, transportadora, transportadora_cnpj,
                destino, data_emissao, data_liberacao, prazo_dias, data_prevista,
                data_entrega_real, entrega_manual, devolvida, tipo_cte, unificado_em,
                devolucao_de, criado_em, atualizado_em
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chave_cte) DO UPDATE SET
                numero_cte=excluded.numero_cte, arquivo=excluded.arquivo,
                transportadora=excluded.transportadora, transportadora_cnpj=excluded.transportadora_cnpj,
                destino=excluded.destino, data_emissao=excluded.data_emissao,
                data_liberacao=COALESCE(excluded.data_liberacao, entregas.data_liberacao),
                prazo_dias=COALESCE(excluded.prazo_dias, entregas.prazo_dias),
                data_prevista=COALESCE(excluded.data_prevista, entregas.data_prevista),
                tipo_cte=excluded.tipo_cte,
                unificado_em=COALESCE(excluded.unificado_em, entregas.unificado_em),
                devolucao_de=COALESCE(entregas.devolucao_de, excluded.devolucao_de),
                atualizado_em=excluded.atualizado_em
        """, (
            result.chave_cte, result.numero_cte, result.arquivo,
            _nome_transportadora_grupo(
                result.transportadora_cnpj,
                result.transportadora_tabela or result.transportadora_nome_cte),
            result.transportadora_cnpj, result.destino, emissao, data_liberacao,
            result.prazo_dias, data_prevista,
            existing["data_entrega_real"] if existing else None,
            existing["entrega_manual"] if existing else 0,
            existing["devolvida"] if existing else 0,
            tipo_cte, unificado_em, devolucao_origem,
            existing["criado_em"] if existing else now, now,
        ))
        # Auto-vínculo achou a ida? Marca a origem como devolvida (fora do OTD).
        if devolucao_origem:
            conn.execute("UPDATE entregas SET devolvida = 1, atualizado_em = ? WHERE chave_cte = ?",
                         (now, devolucao_origem))

        for nf in (result.notas or []):
            numero = (nf.get("numero") or "").lstrip("0")
            if not numero:
                continue
            conn.execute("""
                INSERT OR IGNORE INTO entrega_nfs (chave_cte, chave_nfe, cnpj_emissor, serie, numero_nf, data_entrega)
                VALUES (?,?,?,?,?,NULL)
            """, (
                result.chave_cte, nf.get("chave") or "",
                nf.get("cnpj_emitente") or "", nf.get("serie") or "0", numero,
            ))
            # O CTe real cobre esta NF: descarta o provisório do romaneio (se houver).
            _remover_provisorios_de_nf(conn, nf.get("cnpj_emitente") or "", numero)

        _relink_pendentes(conn, result.chave_cte)
        _recomputar_baixa(conn, result.chave_cte)
        conn.commit()
    finally:
        conn.close()


def _relink_pendentes(conn: sqlite3.Connection, chave_cte: str) -> int:
    """Vincula ocorrências órfãs (chave_cte NULL) às NFs desta entrega e
    aplica as baixas de ENTREGUE/DEVOLUCAO que elas carregam."""
    rows = conn.execute("""
        SELECT o.id, o.categoria, o.data_ocorrencia, n.cnpj_emissor, n.serie, n.numero_nf
        FROM ocorrencias o
        JOIN entrega_nfs n
          ON n.cnpj_emissor = o.cnpj_emissor AND n.numero_nf = o.numero_nf
         AND (COALESCE(NULLIF(ltrim(n.serie,'0'),''),'0') = COALESCE(NULLIF(ltrim(o.serie,'0'),''),'0')
              OR COALESCE(NULLIF(ltrim(o.serie,'0'),''),'0') = '0'
              OR COALESCE(NULLIF(ltrim(n.serie,'0'),''),'0') = '0')
        WHERE o.chave_cte IS NULL AND n.chave_cte = ?
    """, (chave_cte,)).fetchall()

    for r in rows:
        conn.execute("UPDATE ocorrencias SET chave_cte = ? WHERE id = ?", (chave_cte, r["id"]))
        _aplicar_efeito(conn, chave_cte, r["cnpj_emissor"], r["serie"], r["numero_nf"],
                        r["categoria"], r["data_ocorrencia"])
    return len(rows)


def _aplicar_efeito(conn, chave_cte, cnpj, serie, numero, categoria, data_ocorrencia) -> None:
    if categoria == "ENTREGUE" and data_ocorrencia:
        conn.execute("""
            UPDATE entrega_nfs SET data_entrega = ?
            WHERE chave_cte = ? AND cnpj_emissor = ? AND serie = ? AND numero_nf = ?
              AND (data_entrega IS NULL OR data_entrega > ?)
        """, (data_ocorrencia, chave_cte, cnpj, serie, numero, data_ocorrencia))
    elif categoria == "DEVOLUCAO":
        conn.execute("UPDATE entregas SET devolvida = 1 WHERE chave_cte = ?", (chave_cte,))


def _propagar_baixa_para_origem(conn: sqlite3.Connection, chave_filho: str, chave_origem: str) -> None:
    """Copia as datas de entrega das NFs de um CT-e SECUNDÁRIO (reentrega/redespacho/
    complementar unificado) para as mesmas NFs da entrega de ORIGEM, quando a origem
    ainda não tem baixa própria daquela NF. É o que faz a entrega da reentrega fechar
    o CT-e original na Torre em vez de deixá-lo eternamente 'em aberto'."""
    conn.execute("""
        UPDATE entrega_nfs
           SET data_entrega = (
               SELECT MAX(c.data_entrega) FROM entrega_nfs c
                WHERE c.chave_cte = ?
                  AND c.cnpj_emissor = entrega_nfs.cnpj_emissor
                  AND c.serie       = entrega_nfs.serie
                  AND c.numero_nf   = entrega_nfs.numero_nf
                  AND c.data_entrega IS NOT NULL)
         WHERE entrega_nfs.chave_cte = ?
           AND entrega_nfs.data_entrega IS NULL
           AND EXISTS (
               SELECT 1 FROM entrega_nfs c
                WHERE c.chave_cte = ?
                  AND c.cnpj_emissor = entrega_nfs.cnpj_emissor
                  AND c.serie       = entrega_nfs.serie
                  AND c.numero_nf   = entrega_nfs.numero_nf
                  AND c.data_entrega IS NOT NULL)
    """, (chave_filho, chave_origem, chave_filho))


def _recomputar_baixa(conn: sqlite3.Connection, chave_cte: str, _seen: Optional[set] = None) -> None:
    """Entrega concluída quando TODAS as NFs têm data de entrega; a data real
    da entrega é a última baixa. Baixa manual tem precedência. Se o CT-e for
    secundário (unificado em outro), propaga a baixa para a origem e recomputa-a,
    para que a entrega da reentrega feche o CT-e original."""
    if _seen is None:
        _seen = set()
    if chave_cte in _seen:
        return
    _seen.add(chave_cte)
    e = conn.execute(
        "SELECT entrega_manual, data_entrega_real, unificado_em FROM entregas WHERE chave_cte = ?",
        (chave_cte,),
    ).fetchone()
    if e is None:
        return
    if not e["entrega_manual"]:
        agg = conn.execute("""
            SELECT COUNT(*) AS total, COUNT(data_entrega) AS baixadas, MAX(data_entrega) AS ultima
            FROM entrega_nfs WHERE chave_cte = ?
        """, (chave_cte,)).fetchone()
        nova = agg["ultima"] if agg["total"] and agg["total"] == agg["baixadas"] else None
        if nova != e["data_entrega_real"]:
            conn.execute(
                "UPDATE entregas SET data_entrega_real = ?, atualizado_em = ? WHERE chave_cte = ?",
                (nova, datetime.now().isoformat(timespec="seconds"), chave_cte),
            )
    # Secundário: empurra a baixa para a origem e recomputa-a.
    if e["unificado_em"]:
        _propagar_baixa_para_origem(conn, chave_cte, e["unificado_em"])
        _recomputar_baixa(conn, e["unificado_em"], _seen)


# ─── Importação de OCOREN ────────────────────────────────────────────────────

def _chave_nf(cnpj: str, numero: str) -> str:
    cc = re.sub(r"\D", "", cnpj or "")
    nn = (re.sub(r"\D", "", numero or "") or "").lstrip("0") or "0"
    return f"{cc}|{nn}"


def _nfs_com_romaneio_liberado() -> Optional[set]:
    """Chaves 'cnpj|numero' das NFs cujo romaneio já foi LIBERADO (ou encerrado).

    Regra de negócio: só se importa OCOREN de NF que já saiu num romaneio liberado.
    Retorna None quando a trava está desligada (OCOREN_SO_ROMANEIO_LIBERADO=0) — aí
    aceita tudo, como era antes. Se a base de romaneio não puder ser lida, retorna
    conjunto vazio (rejeita tudo — o lado seguro, não processa ocorrência órfã)."""
    if os.getenv("OCOREN_SO_ROMANEIO_LIBERADO", "1").strip().lower() not in ("1", "true", "sim"):
        return None
    try:
        from app import romaneio as _rom
        conn = sqlite3.connect(_rom.DB_PATH)
        try:
            rows = conn.execute(
                "SELECT n.emitente_cnpj, n.nf_numero FROM notas n "
                "JOIN romaneios r ON r.id = n.romaneio_id "
                "WHERE r.situacao IN ('liberado', 'encerrado', 'expedido')"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return set()
    return {_chave_nf(cnpj, numero) for cnpj, numero in rows if cnpj and numero}


def _data_liberacao_por_nf() -> Dict[str, str]:
    """{chave_nf: data_liberacao_iso 'YYYY-MM-DD'} das NFs cujo romaneio já foi
    LIBERADO (quando a carga foi liberada para a transportadora). Se a mesma NF
    aparecer em mais de um romaneio, fica a liberação MAIS RECENTE. Vazio se a base
    de romaneio não puder ser lida."""
    try:
        from app import romaneio as _rom
        conn = sqlite3.connect(_rom.DB_PATH)
        try:
            rows = conn.execute(
                "SELECT n.emitente_cnpj, n.nf_numero, r.data_liberacao FROM notas n "
                "JOIN romaneios r ON r.id = n.romaneio_id "
                "WHERE r.situacao IN ('liberado', 'encerrado', 'expedido') "
                "AND r.data_liberacao IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for cnpj, numero, dlib in rows:
        if not (cnpj and numero and dlib):
            continue
        k = _chave_nf(cnpj, numero)
        d = str(dlib)[:10]
        if k not in out or d > out[k]:
            out[k] = d
    return out


def _liberacao_das_nfs(notas: List[Dict],
                       libs: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Data de LIBERAÇÃO do romaneio das NFs deste CTe (quando a carga foi liberada
    para a transportadora) — base do prazo de entrega e do que a Torre mostra na
    coluna "Liberação". Usa a liberação mais recente entre as NFs. None quando
    nenhuma NF saiu num romaneio liberado (aí prazo/coluna caem para a emissão)."""
    if libs is None:
        libs = _data_liberacao_por_nf()
    datas = []
    for nf in (notas or []):
        k = _chave_nf(nf.get("cnpj_emitente") or nf.get("cnpj_emissor") or "",
                      nf.get("numero") or nf.get("numero_nf") or "")
        if k in libs:
            datas.append(libs[k])
    return max(datas) if datas else None


def _gravar_ocorrencia(conn, cnpj_emissor, serie, numero_nf, codigo, data, hora,
                       codigo_observacao, texto, transportadora_cnpj, arquivo, now,
                       entregas_afetadas: set) -> str:
    """Grava UMA ocorrência (INSERT OR IGNORE), vincula ao CTe pela NF e aplica o
    efeito na entrega. Alimenta `entregas_afetadas`. A classificação e o vínculo
    ficam num só lugar, reusados pela importação normal e pelo reprocessamento de
    pendentes. Retorna 'duplicada' | 'vinculada' | 'sem_vinculo'."""
    info = _classificar(codigo)
    chave_cte = _achar_cte(conn, SimpleNamespace(
        cnpj_emissor=cnpj_emissor, serie=serie, numero_nf=numero_nf))
    cur = conn.execute("""
        INSERT OR IGNORE INTO ocorrencias (
            cnpj_emissor, serie, numero_nf, chave_cte, codigo, descricao, categoria,
            data_ocorrencia, hora, codigo_observacao, texto,
            transportadora_cnpj, arquivo_origem, importado_em
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (cnpj_emissor, serie, numero_nf, chave_cte, codigo,
          info["descricao"], info["categoria"], data, hora,
          codigo_observacao, texto, transportadora_cnpj, arquivo, now))
    if cur.rowcount == 0:
        return "duplicada"
    if chave_cte:
        entregas_afetadas.add(chave_cte)
        # serie da NF na entrega pode diferir da serie do EDI ('0' genérica) —
        # aplica o efeito na NF real vinculada
        nf = conn.execute("""
            SELECT serie FROM entrega_nfs
            WHERE chave_cte = ? AND cnpj_emissor = ? AND numero_nf = ?
        """, (chave_cte, cnpj_emissor, numero_nf)).fetchone()
        serie_real = nf["serie"] if nf else serie
        _aplicar_efeito(conn, chave_cte, cnpj_emissor, serie_real, numero_nf,
                        info["categoria"], data)
        return "vinculada"
    return "sem_vinculo"


def import_ocoren(parsed: OcorenParseResult) -> Dict:
    """Insere as ocorrências de um arquivo OCOREN já parseado.
    Retorna relatório de importação por arquivo.

    Só importa ocorrência de NF que tenha romaneio LIBERADO (regra de negócio);
    as demais ficam em espera (`ocorrencias_pendentes`) e são reprocessadas quando
    o romaneio é liberado, para que o rastreio não se perca."""
    now = datetime.now().isoformat(timespec="seconds")
    novas = duplicadas = vinculadas = sem_vinculo = 0
    rejeitadas_sem_romaneio = 0
    entregas_afetadas: set = set()

    # Antes de importar o arquivo novo, tenta drenar pendentes que já ganharam
    # romaneio liberado — assim uma liberação posterior recupera o rastreio.
    reprocessadas = reprocessar_pendentes()

    nfs_liberadas = _nfs_com_romaneio_liberado()  # None = trava desligada

    conn = _get_conn()
    try:
        for oc in parsed.ocorrencias:
            # Trava: sem romaneio liberado para essa NF, guarda em espera (não descarta).
            if nfs_liberadas is not None and _chave_nf(oc.cnpj_emissor, oc.numero_nf) not in nfs_liberadas:
                conn.execute("""
                    INSERT OR IGNORE INTO ocorrencias_pendentes (
                        cnpj_emissor, serie, numero_nf, codigo, data_ocorrencia, hora,
                        codigo_observacao, texto, transportadora_cnpj, arquivo_origem, guardado_em
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (oc.cnpj_emissor, oc.serie, oc.numero_nf, oc.codigo, oc.data, oc.hora,
                      oc.codigo_observacao, oc.texto, parsed.transportadora_cnpj, parsed.arquivo, now))
                rejeitadas_sem_romaneio += 1
                continue
            r = _gravar_ocorrencia(conn, oc.cnpj_emissor, oc.serie, oc.numero_nf, oc.codigo,
                                   oc.data, oc.hora, oc.codigo_observacao, oc.texto,
                                   parsed.transportadora_cnpj, parsed.arquivo, now, entregas_afetadas)
            if r == "duplicada":
                duplicadas += 1
            elif r == "vinculada":
                novas += 1
                vinculadas += 1
            else:
                novas += 1
                sem_vinculo += 1

        for chave in entregas_afetadas:
            _recomputar_baixa(conn, chave)
        conn.commit()
    finally:
        conn.close()

    return {
        "arquivo": parsed.arquivo,
        "versao": parsed.versao or "?",
        "transportadora_cnpj": parsed.transportadora_cnpj,
        "transportadora_nome": parsed.transportadora_nome,
        "linhas_lidas": parsed.total_linhas,
        "ocorrencias_novas": novas,
        "ocorrencias_duplicadas": duplicadas,
        "rejeitadas_sem_romaneio": rejeitadas_sem_romaneio,
        "reprocessadas_pendentes": reprocessadas,
        "vinculadas": vinculadas,
        "sem_vinculo": sem_vinculo,
        "entregas_atualizadas": len(entregas_afetadas),
        "linhas_rejeitadas": [{"linha": l, "motivo": m} for l, m in parsed.linhas_rejeitadas],
    }


def reprocessar_pendentes() -> int:
    """Reimporta as ocorrências em espera cujas NFs já ganharam romaneio liberado.
    Chamada no início de cada importação e quando um romaneio é liberado, para que
    o rastreio de entregas que receberam OCOREN antes da liberação não se perca.
    Retorna quantas ocorrências saíram da espera e foram gravadas."""
    nfs_liberadas = _nfs_com_romaneio_liberado()
    if not nfs_liberadas:  # None (trava desligada) ou vazio (base ilegível) → nada a fazer
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    gravadas = 0
    entregas_afetadas: set = set()
    conn = _get_conn()
    try:
        pend = conn.execute("""
            SELECT rowid, cnpj_emissor, serie, numero_nf, codigo, data_ocorrencia, hora,
                   codigo_observacao, texto, transportadora_cnpj, arquivo_origem
            FROM ocorrencias_pendentes
        """).fetchall()
        for p in pend:
            if _chave_nf(p["cnpj_emissor"], p["numero_nf"]) not in nfs_liberadas:
                continue  # ainda sem romaneio liberado — segue esperando
            r = _gravar_ocorrencia(conn, p["cnpj_emissor"], p["serie"], p["numero_nf"], p["codigo"],
                                   p["data_ocorrencia"], p["hora"], p["codigo_observacao"], p["texto"],
                                   p["transportadora_cnpj"], p["arquivo_origem"], now, entregas_afetadas)
            # Sai da espera tanto quando grava quanto quando já era duplicada (já está na base).
            conn.execute("DELETE FROM ocorrencias_pendentes WHERE rowid = ?", (p["rowid"],))
            if r != "duplicada":
                gravadas += 1
        for chave in entregas_afetadas:
            _recomputar_baixa(conn, chave)
        conn.commit()
    finally:
        conn.close()
    if gravadas:
        logger.info(f"Reprocessadas {gravadas} ocorrências pendentes (romaneio liberado).")
    return gravadas


def _achar_cte(conn: sqlite3.Connection, oc: OcorrenciaEdi) -> Optional[str]:
    """Localiza o CTe pela NF. Preferência: match exato de série; empate é
    resolvido pela emissão mais recente."""
    rows = conn.execute("""
        SELECT n.chave_cte, n.serie
        FROM entrega_nfs n JOIN entregas e ON e.chave_cte = n.chave_cte
        WHERE n.cnpj_emissor = ? AND n.numero_nf = ?
        ORDER BY (n.serie = ?) DESC, e.data_emissao DESC
    """, (oc.cnpj_emissor, oc.numero_nf, oc.serie)).fetchall()
    if not rows:
        return None
    oc_serie = _norm_serie(oc.serie)
    exatas = [r for r in rows if _norm_serie(r["serie"]) == oc_serie]
    if exatas:
        return exatas[0]["chave_cte"]
    # sem match de série: só aceita se uma das séries for genérica ('0')
    genericas = [r for r in rows if _norm_serie(r["serie"]) == "0" or oc_serie == "0"]
    return genericas[0]["chave_cte"] if genericas else None


# ─── Consulta / status ───────────────────────────────────────────────────────

def _status_entrega(e: Dict, hoje: date) -> str:
    # CT-e de devolução (retorno) — rótulo próprio, fora do OTD. Vale por vínculo
    # (devolucao_de) OU por natureza do CT-e (tipo_cte='DEVOLUCAO'), mesmo que a
    # origem ainda não tenha sido identificada — assim nunca se perde na Torre.
    if e.get("devolucao_de") or e.get("tipo_cte") == "DEVOLUCAO":
        return "DEVOLUCAO"
    # Devolvida tem precedência sobre a entrega: se a mercadoria voltou (devolução
    # vinculada ou ocorrência de devolução), o desfecho é DEVOLVIDA mesmo que já
    # houvesse baixa de entrega — senão o original nunca some do "entregue".
    if e["devolvida"]:
        return "DEVOLVIDA"
    if e["data_entrega_real"]:
        if not e["data_prevista"]:
            return "ENTREGUE"
        return "ENTREGUE_NO_PRAZO" if e["data_entrega_real"] <= e["data_prevista"] else "ENTREGUE_ATRASADO"
    if not e["data_prevista"]:
        return "SEM_PRAZO"
    prevista = date.fromisoformat(e["data_prevista"])
    if hoje > prevista:
        return "ATRASADO"
    if hoje == prevista:
        return "VENCE_HOJE"
    return "EM_TRANSITO"


# Status "em aberto" (não finalizados) — base do marcador "com problema".
_STATUS_ABERTOS = {"EM_TRANSITO", "VENCE_HOJE", "ATRASADO", "SEM_PRAZO"}


def _dias_atraso(e: Dict, hoje: date) -> Optional[int]:
    if not e["data_prevista"]:
        return None
    prevista = date.fromisoformat(e["data_prevista"])
    fim = date.fromisoformat(e["data_entrega_real"]) if e["data_entrega_real"] else hoje
    return max(0, (fim - prevista).days)


def _as_list(v) -> List[str]:
    """Normaliza None / string única / CSV ('A,B') / lista → lista de strings."""
    if not v:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(s).strip() for s in v if str(s).strip()]


def _parse_status_filter(status) -> Optional[set]:
    """Aceita None, string única, CSV ('A,B') ou lista. Retorna set ou None (=todos)."""
    vals = _as_list(status)
    return set(vals) or None


# ─── Alertas: entrega em risco + cobertura de rastreio (#5) ───────────────────
_CAMPOS_RISCO = ("chave_cte", "numero_cte", "transportadora", "destino", "cliente",
                 "numeros_nf", "data_prevista", "status", "dias_atraso",
                 "dias_para_vencer", "ultima_ocorrencia", "ultima_ocorrencia_data")


def entregas_em_risco(dias: int = 2, transportadora=None, mes=None, uf=None) -> List[Dict]:
    """Entregas EM ABERTO cuja previsão vence em ≤`dias` (inclui já vencidas) e que
    ainda NÃO deram sinal de saída para entrega — precisam de ação preventiva antes
    de estourar o prazo. Ordenadas da mais urgente (mais vencida) para a menos."""
    hoje = date.today()
    out = []
    for e in query_entregas(transportadora=transportadora, mes=mes, uf=uf, limit=100000):
        if e["status"] not in ("EM_TRANSITO", "VENCE_HOJE", "ATRASADO"):
            continue
        if e.get("rota"):            # já saiu para entrega → não é o alvo da ação preventiva
            continue
        if not e.get("data_prevista"):
            continue
        dpv = (date.fromisoformat(e["data_prevista"][:10]) - hoje).days
        if dpv <= dias:
            e["dias_para_vencer"] = dpv
            out.append({k: e.get(k) for k in _CAMPOS_RISCO})
    out.sort(key=lambda x: (x["dias_para_vencer"] if x["dias_para_vencer"] is not None else 0))
    return out


def cobertura_rastreio(mes=None, transportadora=None) -> List[Dict]:
    """Por transportadora: % de entregas com ao menos um evento OCOREN. Cobertura
    baixa = transportadora manda pouco EDI → o OTD dela fica cego (parece bom por
    falta de informação). Ordena da pior cobertura para a melhor."""
    por: Dict[str, Dict] = {}
    for e in query_entregas(mes=mes, transportadora=transportadora, limit=100000):
        t = e.get("transportadora") or "—"
        acc = por.setdefault(t, {"total": 0, "com_evento": 0})
        acc["total"] += 1
        if (e.get("total_ocorrencias") or 0) > 0:
            acc["com_evento"] += 1
    linhas = [{
        "transportadora": t, "total": v["total"], "com_evento": v["com_evento"],
        "cobertura_pct": round(100.0 * v["com_evento"] / v["total"], 1) if v["total"] else None,
    } for t, v in por.items()]
    linhas.sort(key=lambda x: (x["cobertura_pct"] if x["cobertura_pct"] is not None else 999))
    return linhas


def query_entregas(
    status=None,
    transportadora: Optional[str] = None,
    mes: Optional[str] = None,
    uf: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
) -> List[Dict]:
    conn = _get_conn()
    q = """
        SELECT e.*,
               (SELECT COUNT(*) FROM entrega_nfs n WHERE n.chave_cte = e.chave_cte) AS total_nfs,
               (SELECT COUNT(data_entrega) FROM entrega_nfs n WHERE n.chave_cte = e.chave_cte) AS nfs_baixadas,
               (SELECT GROUP_CONCAT(numero_nf, ', ') FROM
                    (SELECT DISTINCT numero_nf FROM entrega_nfs
                     WHERE chave_cte = e.chave_cte ORDER BY numero_nf)) AS numeros_nf,
               (SELECT COUNT(DISTINCT o.id) FROM ocorrencias o
                JOIN entrega_nfs n ON n.cnpj_emissor = o.cnpj_emissor AND n.numero_nf = o.numero_nf
                WHERE n.chave_cte = e.chave_cte) AS total_ocorrencias,
               (SELECT o.descricao FROM ocorrencias o
                JOIN entrega_nfs n ON n.cnpj_emissor = o.cnpj_emissor AND n.numero_nf = o.numero_nf
                WHERE n.chave_cte = e.chave_cte AND NOT (o.codigo IN ('99','099','JAMEF-99') AND COALESCE(TRIM(o.texto),'')='')
                ORDER BY o.data_ocorrencia DESC, o.hora DESC, o.id DESC LIMIT 1) AS ultima_ocorrencia,
               (SELECT o.data_ocorrencia FROM ocorrencias o
                JOIN entrega_nfs n ON n.cnpj_emissor = o.cnpj_emissor AND n.numero_nf = o.numero_nf
                WHERE n.chave_cte = e.chave_cte AND NOT (o.codigo IN ('99','099','JAMEF-99') AND COALESCE(TRIM(o.texto),'')='')
                ORDER BY o.data_ocorrencia DESC, o.hora DESC, o.id DESC LIMIT 1) AS ultima_ocorrencia_data,
               (SELECT orig.numero_cte FROM entregas orig WHERE orig.chave_cte = e.devolucao_de) AS devolucao_de_cte,
               (SELECT dev.numero_cte FROM entregas dev WHERE dev.devolucao_de = e.chave_cte LIMIT 1) AS devolvida_por_cte,
               (SELECT dev.chave_cte FROM entregas dev WHERE dev.devolucao_de = e.chave_cte LIMIT 1) AS devolvida_por_chave,
               (SELECT dev.data_entrega_real FROM entregas dev WHERE dev.devolucao_de = e.chave_cte LIMIT 1) AS retorno_recebido_em,
               (SELECT GROUP_CONCAT(u.numero_cte || ' (' || lower(u.tipo_cte) || ')', ', ')
                FROM entregas u WHERE u.unificado_em = e.chave_cte) AS ctes_unificados,
               (SELECT o.descricao FROM ocorrencias o
                JOIN entrega_nfs n ON n.cnpj_emissor = o.cnpj_emissor AND n.numero_nf = o.numero_nf
                WHERE n.chave_cte = e.chave_cte AND o.categoria = 'TENTATIVA_FRUSTRADA'
                ORDER BY o.data_ocorrencia DESC, o.hora DESC, o.id DESC LIMIT 1) AS problema_desc,
               (SELECT o.data_ocorrencia FROM ocorrencias o
                JOIN entrega_nfs n ON n.cnpj_emissor = o.cnpj_emissor AND n.numero_nf = o.numero_nf
                WHERE n.chave_cte = e.chave_cte AND o.categoria = 'TENTATIVA_FRUSTRADA'
                ORDER BY o.data_ocorrencia DESC, o.hora DESC, o.id DESC LIMIT 1) AS problema_data
        FROM entregas e WHERE e.unificado_em IS NULL
    """
    params: list = []
    transp_list = _as_list(transportadora)
    if transp_list:
        q += f" AND e.transportadora IN ({','.join('?' * len(transp_list))})"
        params.extend(transp_list)
    mes_list = _as_list(mes)
    if mes_list:
        q += f" AND substr(e.data_emissao, 1, 7) IN ({','.join('?' * len(mes_list))})"
        params.extend(mes_list)
    uf_list = [u.strip().upper() for u in _as_list(uf) if u and u.strip()]
    if uf_list:
        # UF = últimos 2 caracteres do destino ("CIDADE/UF" ou só "UF").
        q += f" AND UPPER(SUBSTR(TRIM(e.destino), -2)) IN ({','.join('?' * len(uf_list))})"
        params.extend(uf_list)
    if search:
        s = f"%{search}%"
        q += """ AND (e.numero_cte LIKE ? OR e.chave_cte LIKE ? OR e.arquivo LIKE ? OR e.destino LIKE ?
                 OR EXISTS (SELECT 1 FROM entrega_nfs n WHERE n.chave_cte = e.chave_cte AND n.numero_nf LIKE ?))"""
        params.extend([s, s, s, s, s])
    q += " ORDER BY e.data_emissao DESC, e.numero_cte DESC LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in conn.execute(q, params).fetchall()]

    # Tentativas de entrega (TENTATIVA_FRUSTRADA) por entrega — 1 query para todas
    # as linhas. Base do OTD ajustado/potencial e da sinalização de re-tentativas.
    tent_por_chave: Dict[str, list] = {}
    chaves = [e["chave_cte"] for e in rows]
    if chaves:
        ph = ",".join("?" * len(chaves))
        for o in conn.execute(f"""
            SELECT n.chave_cte, o.codigo, o.data_ocorrencia
            FROM ocorrencias o
            JOIN entrega_nfs n ON n.cnpj_emissor = o.cnpj_emissor AND n.numero_nf = o.numero_nf
            WHERE o.categoria = 'TENTATIVA_FRUSTRADA' AND n.chave_cte IN ({ph})
            ORDER BY o.data_ocorrencia, o.hora, o.id
        """, chaves):
            tent_por_chave.setdefault(o["chave_cte"], []).append(
                (o["codigo"], (o["data_ocorrencia"] or "")[:10])
            )
    conn.close()

    status_set = _parse_status_filter(status)
    hoje = date.today()
    out = []
    for e in rows:
        e["status"] = _status_entrega(e, hoje)
        e["dias_atraso"] = _dias_atraso(e, hoje)
        # CT-e de devolução: a carga retorna ao nosso hub — o destino exibido é
        # a Serra/ES (o CT-e traz o cliente como destinatário, mas fisicamente a
        # mercadoria volta pra nós).
        if e["status"] == "DEVOLUCAO":
            e["destino"] = "Serra/ES"
        # "Com problema" = teve tentativa frustrada (recusa/fechado/ausente/reentrega)
        # e a entrega ainda está EM ABERTO (não entregue/devolvida) — precisa de ação.
        e["problema"] = bool(e.get("problema_desc")) and e["status"] in _STATUS_ABERTOS
        # "Em rota de entrega" = a ÚLTIMA ocorrência é 'saída para entrega' e a
        # entrega ainda está em aberto — sai da lista assim que entregar/frustrar.
        e["rota"] = e["status"] in _STATUS_ABERTOS and _eh_rota_entrega(e.get("ultima_ocorrencia") or "")
        _aplicar_flags_tentativa(e, tent_por_chave.get(e["chave_cte"], []))
        if status_set is None or e["status"] in status_set:
            out.append(e)
    return out


def _aplicar_flags_tentativa(e: Dict, tentativas: list) -> None:
    """Sinaliza (read-only, sem mudar o OTD atual) as tentativas de entrega:
      - num_tentativas / primeira_tentativa / dias_pos_tentativa
      - no_prazo_ajustado: no prazo atual OU tentou no prazo por culpa do CLIENTE
        (ausente/fechado/recusa) — perdoa a transportadora.
      - no_prazo_potencial: se a 1ª tentativa (ou a entrega, se não houve tentativa)
        caiu dentro do prazo — o teto 'se tudo fechasse na 1ª tentativa'."""
    culpa = load_culpa()
    prev = (e.get("data_prevista") or "")[:10]
    real = (e.get("data_entrega_real") or "")[:10]
    no_prazo_atual = bool(real and prev and real <= prev)

    e["num_tentativas"] = len(tentativas)
    e["primeira_tentativa"] = tentativas[0][1] if tentativas else None
    e["dias_pos_tentativa"] = None
    if tentativas and real and e["primeira_tentativa"]:
        try:
            e["dias_pos_tentativa"] = max(
                0, (date.fromisoformat(real) - date.fromisoformat(e["primeira_tentativa"])).days
            )
        except (ValueError, TypeError):
            pass

    cliente_no_prazo = any(
        _normalizar_codigo(cod) in culpa["cliente"] and d and prev and d <= prev
        for cod, d in tentativas
    )
    e["tentativa_cliente_no_prazo"] = cliente_no_prazo
    e["no_prazo_ajustado"] = no_prazo_atual or cliente_no_prazo
    prim_ou_real = e["primeira_tentativa"] or real
    e["no_prazo_potencial"] = bool(prim_ou_real and prev and prim_ou_real <= prev)


def timeline(chave_cte: str) -> Optional[Dict]:
    conn = _get_conn()
    e = conn.execute("SELECT * FROM entregas WHERE chave_cte = ?", (chave_cte,)).fetchone()
    if e is None:
        conn.close()
        return None
    nfs = conn.execute(
        "SELECT * FROM entrega_nfs WHERE chave_cte = ? ORDER BY numero_nf", (chave_cte,)
    ).fetchall()
    # Ocorrências POR NF (cnpj+número) desta entrega — mostra o histórico mesmo
    # quando a NF está em mais de um CTe (redespacho/devolução) e a ocorrência foi
    # gravada sob outro CTe. DISTINCT para não repetir se a NF tiver 2 séries.
    ocs = conn.execute("""
        SELECT DISTINCT o.* FROM ocorrencias o
        JOIN entrega_nfs n ON n.cnpj_emissor = o.cnpj_emissor AND n.numero_nf = o.numero_nf
        WHERE n.chave_cte = ?
        ORDER BY o.data_ocorrencia, o.hora, o.id
    """, (chave_cte,)).fetchall()
    conn.close()

    hoje = date.today()
    entrega = dict(e)
    entrega["status"] = _status_entrega(entrega, hoje)
    entrega["dias_atraso"] = _dias_atraso(entrega, hoje)
    return {
        "entrega": entrega,
        "nfs": [dict(n) for n in nfs],
        "ocorrencias": [dict(o) for o in ocs],
    }


def marcar_entrega_manual(chave_cte: str, data_entrega: Optional[str]) -> bool:
    """data_entrega ISO -> baixa manual; None -> remove a baixa manual e volta
    a valer o cálculo pelas ocorrências."""
    conn = _get_conn()
    try:
        if data_entrega:
            cur = conn.execute(
                "UPDATE entregas SET data_entrega_real = ?, entrega_manual = 1, atualizado_em = ? WHERE chave_cte = ?",
                (data_entrega, datetime.now().isoformat(timespec="seconds"), chave_cte),
            )
        else:
            cur = conn.execute(
                "UPDATE entregas SET data_entrega_real = NULL, entrega_manual = 0 WHERE chave_cte = ?",
                (chave_cte,),
            )
            if cur.rowcount:
                _recomputar_baixa(conn, chave_cte)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def marcar_devolvida_por_nf(numero_nf: str, serie: Optional[str] = None,
                            cnpj_emissor: Optional[str] = None) -> Dict:
    """Marca como DEVOLVIDA a(s) entrega(s) que contêm a NF de VENDA informada.

    Usado pelo painel de Análise de Vendas quando uma devolução é casada: reflete o
    status na Torre para controlar as devoluções em andamento. Idempotente — só marca
    as que ainda não estão devolvidas. `numero_nf` é normalizado (dígitos, sem zeros à
    esquerda) para bater com `entrega_nfs.numero_nf`. Retorna
    {encontradas, marcadas, ja_devolvidas, chaves}."""
    numero = re.sub(r"\D", "", str(numero_nf or "")).lstrip("0")
    if not numero:
        return {"encontradas": 0, "marcadas": 0, "ja_devolvidas": 0, "chaves": []}
    conds, params = ["numero_nf = ?"], [numero]
    if serie:
        conds.append("serie = ?"); params.append(str(serie))
    if cnpj_emissor:
        conds.append("cnpj_emissor = ?"); params.append(re.sub(r"\D", "", str(cnpj_emissor)))
    conn = _get_conn()
    try:
        chaves = [r["chave_cte"] for r in conn.execute(
            f"SELECT DISTINCT chave_cte FROM entrega_nfs WHERE {' AND '.join(conds)}", params).fetchall()]
        marcadas, ja = [], 0
        agora = datetime.now().isoformat(timespec="seconds")
        for ch in chaves:
            cur = conn.execute(
                "UPDATE entregas SET devolvida = 1, atualizado_em = ? WHERE chave_cte = ? AND COALESCE(devolvida,0) = 0",
                (agora, ch))
            if cur.rowcount:
                marcadas.append(ch)
            else:
                ja += 1
        conn.commit()
        return {"encontradas": len(chaves), "marcadas": len(marcadas), "ja_devolvidas": ja, "chaves": chaves}
    finally:
        conn.close()


# ─── Bucketização de OTD (fonte única — usada por kpis e pelo scorecard) ──────
# Agrupamento dos status por efeito no OTD. Mantido num só lugar para que a
# Torre (kpis) e o Scorecard (_agrega) nunca divirjam na conta.
STATUS_EM_TRANSITO = ("EM_TRANSITO", "VENCE_HOJE")


def novo_bucket() -> Dict:
    return {"total": 0, "entregues": 0, "no_prazo": 0, "entregue_atrasado": 0,
            "entregue_sem_prazo": 0, "atrasadas_abertas": 0, "em_transito": 0,
            "devolvidas": 0, "sem_prazo": 0, "soma_atraso": 0, "com_atraso": 0,
            "com_problema": 0, "no_prazo_ajustado": 0, "no_prazo_potencial": 0}


def classificar_entrega(b: Dict, e: Dict) -> None:
    """Acumula uma entrega no bucket conforme seu status calculado.

    OTD só considera entregas que TINHAM prazo contratado (no_prazo + atrasado).
    'ENTREGUE' (entregue sem prazo de tabela) é contado à parte em
    `entregue_sem_prazo` e fica FORA do OTD — não infla nem penaliza o indicador.

    O CT-e de DEVOLUÇÃO (retorno) é informativo e NÃO entra em nenhum KPI: a
    devolução daquela mercadoria já é contada uma única vez, no CT-e original
    (status DEVOLVIDA). Sem isto a mesma NF contaria como 2 devoluções."""
    st = e["status"]
    if st == "DEVOLUCAO":
        return
    b["total"] += 1
    if st == "ENTREGUE_NO_PRAZO":
        b["entregues"] += 1
        b["no_prazo"] += 1
        b["no_prazo_ajustado"] += 1
        b["no_prazo_potencial"] += 1
    elif st == "ENTREGUE":  # entregue, porém sem prazo contratado — fora do OTD
        b["entregues"] += 1
        b["entregue_sem_prazo"] += 1
    elif st == "ENTREGUE_ATRASADO":
        b["entregues"] += 1
        b["entregue_atrasado"] += 1
        # Sinalização read-only: crédito ajustado/potencial (não altera o OTD atual).
        if e.get("no_prazo_ajustado"):
            b["no_prazo_ajustado"] += 1
        if e.get("no_prazo_potencial"):
            b["no_prazo_potencial"] += 1
        if e.get("dias_atraso"):
            b["soma_atraso"] += e["dias_atraso"]
            b["com_atraso"] += 1
    elif st == "ATRASADO":
        b["atrasadas_abertas"] += 1
    elif st in STATUS_EM_TRANSITO:
        b["em_transito"] += 1
    elif st == "DEVOLVIDA":
        b["devolvidas"] += 1
    elif st == "SEM_PRAZO":
        b["sem_prazo"] += 1


def avaliadas_otd(b: Dict) -> int:
    """Entregas que tinham prazo para cumprir (base do OTD)."""
    return b["no_prazo"] + b["entregue_atrasado"]


def otd_do_bucket(b: Dict) -> Optional[float]:
    base = avaliadas_otd(b)
    return round(b["no_prazo"] / base * 100, 1) if base else None


def otd_ajustado_do_bucket(b: Dict) -> Optional[float]:
    """OTD que perdoa a 1ª tentativa no prazo por culpa do cliente (read-only)."""
    base = avaliadas_otd(b)
    return round(b["no_prazo_ajustado"] / base * 100, 1) if base else None


def otd_potencial_do_bucket(b: Dict) -> Optional[float]:
    """OTD teto: se toda entrega fechasse na 1ª tentativa (read-only)."""
    base = avaliadas_otd(b)
    return round(b["no_prazo_potencial"] / base * 100, 1) if base else None


def atraso_medio_do_bucket(b: Dict) -> float:
    return round(b["soma_atraso"] / b["com_atraso"], 1) if b["com_atraso"] else 0


def reprocessar_datas_previstas() -> Dict:
    """Preenche `data_liberacao` (liberação do romaneio das NFs) e recalcula
    `data_prevista` = liberação + prazo_dias, com a regra de dias úteis ATUAL
    (feriados incluídos). A base do prazo é a LIBERAÇÃO DO ROMANEIO (quando a carga
    foi liberada); cai para a emissão do CTe quando a NF não saiu em romaneio
    liberado. Útil após ligar/editar feriados OU liberar romaneios: corrige a
    coluna Liberação e o OTD de registros já gravados sem re-auditar os XMLs. Não
    altera prazo_dias nem baixas. Retorna total analisado e quantas datas mudaram."""
    libs = _data_liberacao_por_nf()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT chave_cte, data_emissao, data_liberacao, prazo_dias, data_prevista "
            "FROM entregas"
        ).fetchall()
        now = datetime.now().isoformat(timespec="seconds")
        analisadas = alteradas = 0
        for r in rows:
            analisadas += 1
            nfs = conn.execute(
                "SELECT cnpj_emissor, numero_nf FROM entrega_nfs WHERE chave_cte = ?",
                (r["chave_cte"],),
            ).fetchall()
            notas = [{"cnpj_emissor": n["cnpj_emissor"], "numero_nf": n["numero_nf"]} for n in nfs]
            # Provisórios de romaneio já nascem com data_liberacao; para os demais,
            # busca a liberação pela NF (fica nula se a NF não saiu em romaneio).
            liberacao = _liberacao_das_nfs(notas, libs) or r["data_liberacao"]
            base = liberacao or r["data_emissao"]
            nova_prevista = r["data_prevista"]
            if r["prazo_dias"] and base:
                try:
                    nova_prevista = somar_dias_uteis(
                        date.fromisoformat(base[:10]), int(r["prazo_dias"])
                    ).isoformat()
                except (ValueError, TypeError):
                    pass
            if liberacao != r["data_liberacao"] or nova_prevista != r["data_prevista"]:
                conn.execute(
                    "UPDATE entregas SET data_liberacao = ?, data_prevista = ?, "
                    "atualizado_em = ? WHERE chave_cte = ?",
                    (liberacao, nova_prevista, now, r["chave_cte"]),
                )
                alteradas += 1
        conn.commit()
    finally:
        conn.close()
    return {"analisadas": analisadas, "alteradas": alteradas}


def _dest_cep_por_nf() -> Dict[str, str]:
    """{chave_nf: CEP de destino (8 dígitos)} a partir das notas do romaneio — para
    re-derivar o prazo pela faixa de CEP (mais preciso que só cidade/UF)."""
    try:
        from app import romaneio as _rom
        conn = sqlite3.connect(_rom.DB_PATH)
        try:
            rows = conn.execute(
                "SELECT emitente_cnpj, nf_numero, dest_cep FROM notas WHERE dest_cep IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for cnpj, numero, cep in rows:
        c = re.sub(r"\D", "", str(cep or ""))
        if cnpj and numero and c:
            out[_chave_nf(cnpj, numero)] = c.zfill(8)
    return out


def reprocessar_prazos_cadastro(prazos: Dict) -> Dict:
    """Re-deriva `prazo_dias` de cada entrega a partir da tabela de prazos ATUAL do
    cadastro (por transportadora + destino) e recalcula `data_prevista`. É o elo que
    faz uma edição da tabela de prazo chegar à Torre: o prazo é gravado como snapshot
    na auditoria e, sozinho, não acompanha mudanças da tabela. Casa por faixa de CEP
    (via nota do romaneio) e, na falta, por cidade/UF do destino. `prazos`:
    {transportadora: DataFrame} — o `_prazos` do app. Não mexe em baixas.
    Retorna total analisado e quantos prazos mudaram."""
    from app import rate_calculator as rc
    ceps = _dest_cep_por_nf()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT chave_cte, transportadora, destino, data_emissao, data_liberacao, "
            "prazo_dias, data_prevista FROM entregas"
        ).fetchall()
        now = datetime.now().isoformat(timespec="seconds")
        analisadas = alteradas = 0
        for r in rows:
            analisadas += 1
            df = prazos.get(r["transportadora"]) if prazos else None
            if df is None or getattr(df, "empty", True):
                continue
            # CEP da NF (via romaneio) tem precedência; senão cidade/UF do destino.
            prazo_row = None
            nfs = conn.execute(
                "SELECT cnpj_emissor, numero_nf FROM entrega_nfs WHERE chave_cte = ?",
                (r["chave_cte"],),
            ).fetchall()
            for n in nfs:
                cep = ceps.get(_chave_nf(n["cnpj_emissor"], n["numero_nf"]))
                if cep:
                    prazo_row = rc._match_prazos_by_cep(df, cep)
                    if prazo_row is not None:
                        break
            if prazo_row is None and r["destino"]:
                city, _sep, uf = (r["destino"] or "").rpartition("/")
                if city:
                    prazo_row = rc._match_prazos_by_city(df, city.strip(), uf.strip())
            if prazo_row is None:
                continue
            try:
                novo = int(prazo_row.get("PRAZO_DIAS", 0) or 0)
            except (ValueError, TypeError):
                continue
            if novo <= 0 or novo == (r["prazo_dias"] or 0):
                continue
            base = r["data_liberacao"] or r["data_emissao"]
            nova_prev = r["data_prevista"]
            if base:
                try:
                    nova_prev = somar_dias_uteis(date.fromisoformat(base[:10]), novo).isoformat()
                except (ValueError, TypeError):
                    pass
            conn.execute(
                "UPDATE entregas SET prazo_dias = ?, data_prevista = ?, atualizado_em = ? "
                "WHERE chave_cte = ?",
                (novo, nova_prev, now, r["chave_cte"]),
            )
            alteradas += 1
        conn.commit()
    finally:
        conn.close()
    return {"analisadas": analisadas, "alteradas": alteradas}


def religar_ocorrencias_orfas() -> Dict:
    """Repara ocorrências cujo `chave_cte` aponta para uma entrega que não existe
    mais — tipicamente um provisório de romaneio apagado quando o CTe real chegou.
    Volta-as a pendentes (NULL) e religa por NF à entrega vigente, preferindo o
    CTe real. Reaplica as baixas de ENTREGUE/DEVOLUCAO. Retorna quantas foram
    desligadas e religadas."""
    conn = _get_conn()
    try:
        desligadas = conn.execute(
            "UPDATE ocorrencias SET chave_cte = NULL "
            "WHERE chave_cte IS NOT NULL "
            "  AND chave_cte NOT IN (SELECT chave_cte FROM entregas)"
        ).rowcount
        # CTes reais primeiro (têm preferência); provisórios de romaneio depois.
        reais = [r["chave_cte"] for r in conn.execute(
            "SELECT chave_cte FROM entregas WHERE chave_cte NOT LIKE 'ROM:%'"
        ).fetchall()]
        provisorios = [r["chave_cte"] for r in conn.execute(
            "SELECT chave_cte FROM entregas WHERE chave_cte LIKE 'ROM:%'"
        ).fetchall()]
        religadas = 0
        for ch in reais + provisorios:
            religadas += _relink_pendentes(conn, ch)
            _recomputar_baixa(conn, ch)
        conn.commit()
    finally:
        conn.close()
    return {"desligadas": desligadas, "religadas": religadas}


def reclassificar_ocorrencias() -> Dict:
    """Re-aplica a tabela de códigos ATUAL às ocorrências já gravadas: atualiza a
    descrição e a categoria dos códigos que mudaram (ex.: um código antes 'não
    mapeado' que agora tem legenda) e reaplica as baixas de ENTREGUE/DEVOLUCAO nas
    entregas vinculadas. Use após editar/recarregar ocorrencias_codigos.json — a
    classificação é gravada no momento da importação e não se atualiza sozinha.
    Retorna quantas ocorrências mudaram e quantas seguem sem mapeamento."""
    load_codigos(force=True)  # garante a versão em disco mais recente
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, codigo, chave_cte, cnpj_emissor, serie, numero_nf, "
            "descricao, categoria, data_ocorrencia FROM ocorrencias"
        ).fetchall()
        atualizadas = 0
        sem_mapa = 0
        afetadas: set = set()
        for r in rows:
            info = _classificar(r["codigo"])
            if "não mapeado" in info["descricao"]:
                sem_mapa += 1
            if info["descricao"] == r["descricao"] and info["categoria"] == r["categoria"]:
                continue
            conn.execute(
                "UPDATE ocorrencias SET descricao = ?, categoria = ? WHERE id = ?",
                (info["descricao"], info["categoria"], r["id"]),
            )
            atualizadas += 1
            if r["chave_cte"]:
                nf = conn.execute(
                    "SELECT serie FROM entrega_nfs WHERE chave_cte = ? AND cnpj_emissor = ? AND numero_nf = ?",
                    (r["chave_cte"], r["cnpj_emissor"], r["numero_nf"]),
                ).fetchone()
                serie_real = nf["serie"] if nf else r["serie"]
                _aplicar_efeito(conn, r["chave_cte"], r["cnpj_emissor"], serie_real,
                                r["numero_nf"], info["categoria"], r["data_ocorrencia"])
                afetadas.add(r["chave_cte"])
        for ch in afetadas:
            _recomputar_baixa(conn, ch)
        conn.commit()
    finally:
        conn.close()
    return {"atualizadas": atualizadas, "sem_mapeamento": sem_mapa}


def recanonizar_transportadoras() -> Dict:
    """Aplica o AGRUPAMENTO de transportadora por CNPJ às entregas já gravadas:
    resolve o nome de cada uma pelo grupo (mapa CNPJ→nome, exato ou pela raiz do
    CNPJ) e unifica as filiais/logística reversa sob o mesmo nome. Use após editar
    os CNPJs de uma transportadora no cadastro. Retorna quantas foram unificadas."""
    from app import carrier_mapping as _cm
    mapping = _cm.load_mapping()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT chave_cte, transportadora, transportadora_cnpj FROM entregas "
            "WHERE transportadora_cnpj IS NOT NULL AND transportadora_cnpj <> ''"
        ).fetchall()
        now = datetime.now().isoformat(timespec="seconds")
        alteradas = 0
        for r in rows:
            novo = _nome_transportadora_grupo(r["transportadora_cnpj"], r["transportadora"], mapping)
            if novo and novo != r["transportadora"]:
                conn.execute(
                    "UPDATE entregas SET transportadora = ?, atualizado_em = ? WHERE chave_cte = ?",
                    (novo, now, r["chave_cte"]),
                )
                alteradas += 1
        conn.commit()
    finally:
        conn.close()
    return {"unificadas": alteradas}


def _classifica_cte_data(cd) -> Optional[str]:
    if str(getattr(cd, "tp_cte", "0") or "0") == "1":
        return "COMPLEMENTAR"
    if str(getattr(cd, "tp_serv", "0") or "0") in ("2", "3"):
        return "REDESPACHO"
    if (getattr(cd, "caracteristica_adicional", "") or "").strip().upper() == "REENTREGA":
        return "REENTREGA"
    return None


def reprocessar_unificacao() -> Dict:
    """Classifica os CT-es já gravados como secundários (reentrega/complementar/
    redespacho) re-lendo os XMLs disponíveis em cte_xml/, e os unifica no CT-e de
    origem (mesma NF). Devolução NÃO é unificada. Retorna quantos foram unificados e
    quantos ficaram sem origem encontrada. Idempotente."""
    from app import cte_parser
    base_dir = Path(__file__).resolve().parent.parent / "cte_xml"
    # índice {chave: CteData} a partir dos XMLs disponíveis
    idx: Dict[str, object] = {}
    if base_dir.exists():
        for f in base_dir.rglob("*.xml"):
            try:
                cd = cte_parser.parse_cte_bytes(f.read_bytes(), f.name)
                if cd.chave:
                    idx[re.sub(r"\D", "", cd.chave)] = cd
            except Exception:
                continue

    conn = _get_conn()
    unificados = sem_origem = 0
    try:
        rows = conn.execute(
            "SELECT chave_cte FROM entregas WHERE chave_cte NOT LIKE 'ROM:%' AND devolucao_de IS NULL"
        ).fetchall()
        now = datetime.now().isoformat(timespec="seconds")
        for r in rows:
            cd = idx.get(re.sub(r"\D", "", r["chave_cte"]))
            if cd is None:
                continue
            tipo = _classifica_cte_data(cd)
            if not tipo:
                continue
            nfs = conn.execute(
                "SELECT cnpj_emissor, numero_nf FROM entrega_nfs WHERE chave_cte = ?",
                (r["chave_cte"],),
            ).fetchall()
            notas = [{"cnpj_emissor": n["cnpj_emissor"], "numero_nf": n["numero_nf"]} for n in nfs]
            origem = _achar_origem_unificacao(
                conn, notas, r["chave_cte"], getattr(cd, "cte_comp_chave", None), tipo)
            if origem:
                conn.execute(
                    "UPDATE entregas SET tipo_cte = ?, unificado_em = ?, atualizado_em = ? WHERE chave_cte = ?",
                    (tipo, origem, now, r["chave_cte"]),
                )
                unificados += 1
            else:
                conn.execute(
                    "UPDATE entregas SET tipo_cte = ? WHERE chave_cte = ?", (tipo, r["chave_cte"]))
                sem_origem += 1
        fechadas = _propagar_baixas_unificadas(conn)
        conn.commit()
    finally:
        conn.close()
    return {"unificados": unificados, "sem_origem": sem_origem, "origens_fechadas": fechadas}


def _propagar_baixas_unificadas(conn: sqlite3.Connection) -> int:
    """Para todo CT-e secundário (unificado em outro), empurra a baixa das suas NFs
    entregues para a entrega de origem e recomputa a origem. Repara CT-es de origem
    que ficaram 'em aberto' porque a entrega foi registrada no CT-e da reentrega.
    Retorna quantas origens passaram a ter data de entrega real."""
    filhos = conn.execute(
        "SELECT chave_cte, unificado_em FROM entregas "
        "WHERE unificado_em IS NOT NULL AND entrega_manual = 0"
    ).fetchall()
    origens = set()
    for f in filhos:
        _propagar_baixa_para_origem(conn, f["chave_cte"], f["unificado_em"])
        origens.add(f["unificado_em"])
    fechadas = 0
    for ch in origens:
        antes = conn.execute(
            "SELECT data_entrega_real FROM entregas WHERE chave_cte = ?", (ch,)
        ).fetchone()
        _recomputar_baixa(conn, ch)
        depois = conn.execute(
            "SELECT data_entrega_real FROM entregas WHERE chave_cte = ?", (ch,)
        ).fetchone()
        if antes and depois and not antes["data_entrega_real"] and depois["data_entrega_real"]:
            fechadas += 1
    return fechadas


def reprocessar_baixas_unificadas() -> Dict:
    """Varredura pública: propaga as baixas dos CT-es secundários para as origens.
    Idempotente. Use quando uma reentrega/redespacho já entregue deixou o CT-e
    original aberto na Torre."""
    conn = _get_conn()
    try:
        fechadas = _propagar_baixas_unificadas(conn)
        conn.commit()
    finally:
        conn.close()
    return {"origens_fechadas": fechadas}


def reprocessar_devolucoes() -> Dict:
    """Casa os CT-es de DEVOLUÇÃO com o CT-e de IDA (origem) e os marca como
    devolução na Torre. Match: 1º pela NF; 2º pelo CLIENTE (remetente da devolução
    = destinatário da ida), lendo o remetente do XML. Todo CT-e de devolução é
    marcado (tipo_cte='DEVOLUCAO') mesmo sem origem — assim nunca se perde. Não
    sobrescreve vínculo já existente (manual). Retorna as contagens."""
    from app import cte_parser
    base_dir = Path(__file__).resolve().parent.parent / "cte_xml"
    rem_por_chave: Dict[str, str] = {}
    if base_dir.exists():
        for f in base_dir.rglob("*.xml"):
            try:
                cd = cte_parser.parse_cte_bytes(f.read_bytes(), f.name)
                if cd.chave:
                    rem_por_chave[re.sub(r"\D", "", cd.chave)] = getattr(cd, "remetente_cnpj", "") or ""
            except Exception:
                continue

    # 1) passe de LEITURA — resolve a origem de cada devolução (buscar_valor_ida
    #    abre a própria conexão; por isso não misturamos com a escrita).
    conn_r = _get_conn()
    devs = conn_r.execute("""
        SELECT e.chave_cte, e.data_emissao, e.devolucao_de FROM entregas e
        JOIN audit_history h ON h.chave_cte = e.chave_cte
        WHERE h.tipo_operacao = 'DEVOLUCAO' AND e.chave_cte NOT LIKE 'ROM:%'
    """).fetchall()
    plano = []  # (chave_dev, origem_ou_None, ja_linkada)
    for d in devs:
        if d["devolucao_de"]:
            plano.append((d["chave_cte"], None, True))
            continue
        ida = None
        for nf in conn_r.execute(
            "SELECT cnpj_emissor, serie, numero_nf FROM entrega_nfs WHERE chave_cte = ?", (d["chave_cte"],)):
            ida = buscar_valor_ida(nf["numero_nf"], nf["serie"], nf["cnpj_emissor"], excluir_chave_cte=d["chave_cte"])
            if ida:
                break
        if not ida:
            rem = rem_por_chave.get(re.sub(r"\D", "", d["chave_cte"]))
            if rem:
                ida = buscar_valor_ida_por_cliente(rem, antes_de=d["data_emissao"], excluir_chave_cte=d["chave_cte"])
        origem = ida.get("chave_cte") if ida else None
        if origem and not conn_r.execute("SELECT 1 FROM entregas WHERE chave_cte = ?", (origem,)).fetchone():
            origem = None
        plano.append((d["chave_cte"], origem, False))
    conn_r.close()

    # 2) passe de ESCRITA
    conn = _get_conn()
    devolucoes = vinculadas = sem_origem = 0
    try:
        now = datetime.now().isoformat(timespec="seconds")
        for chave_dev, origem, ja in plano:
            devolucoes += 1
            conn.execute(
                "UPDATE entregas SET tipo_cte = 'DEVOLUCAO', atualizado_em = ? "
                "WHERE chave_cte = ? AND (tipo_cte IS NULL OR tipo_cte <> 'DEVOLUCAO')",
                (now, chave_dev))
            if ja:
                vinculadas += 1
            elif origem:
                conn.execute("UPDATE entregas SET devolucao_de = ?, atualizado_em = ? WHERE chave_cte = ?",
                             (origem, now, chave_dev))
                conn.execute("UPDATE entregas SET devolvida = 1, atualizado_em = ? WHERE chave_cte = ?",
                             (now, origem))
                vinculadas += 1
            else:
                sem_origem += 1
        conn.commit()
    finally:
        conn.close()
    ref = casar_devolucoes_por_referencia()
    return {"devolucoes": devolucoes, "vinculadas": vinculadas + ref["vinculadas"],
            "sem_origem": sem_origem, "por_referencia": ref["vinculadas"]}


def casar_devolucoes_por_referencia() -> Dict:
    """Casa devoluções pela NF-e REFERENCIADA (capturada do XML da NF-e no Olist):
    uma nota com finalidade '4' (devolução) e nf_ref_chave apontando a NF de origem
    liga o CT-e que carrega a devolução ao CT-e da NF original. Chave de vínculo mais
    confiável que o casamento por valor/cliente. Retorna quantas foram vinculadas."""
    try:
        from app import romaneio as _rom
        r = sqlite3.connect(_rom.DB_PATH)
        r.row_factory = sqlite3.Row
        try:
            devs = r.execute(
                "SELECT nf_numero, emitente_cnpj, nf_ref_chave FROM notas "
                "WHERE nf_finalidade = '4' AND COALESCE(nf_ref_chave, '') <> ''"
            ).fetchall()
            origem_por_chave = {
                re.sub(r"\D", "", row["chave"] or ""): (row["nf_numero"], row["emitente_cnpj"])
                for row in r.execute("SELECT chave, nf_numero, emitente_cnpj FROM notas")
            }
        finally:
            r.close()
    except Exception:
        return {"vinculadas": 0, "sem_par": 0}

    conn = _get_conn()
    vinculadas = sem_par = 0
    try:
        now = datetime.now().isoformat(timespec="seconds")
        for d in devs:
            num = str(d["nf_numero"] or "").lstrip("0")
            cnpj = re.sub(r"\D", "", d["emitente_cnpj"] or "")
            dev = conn.execute(
                "SELECT e.chave_cte FROM entrega_nfs n JOIN entregas e ON e.chave_cte = n.chave_cte "
                "WHERE n.numero_nf = ? AND n.cnpj_emissor = ? AND e.chave_cte NOT LIKE 'ROM:%' "
                "AND e.devolucao_de IS NULL LIMIT 1", (num, cnpj)).fetchone()
            orig_nf = origem_por_chave.get(re.sub(r"\D", "", d["nf_ref_chave"] or ""))
            if not dev or not orig_nf:
                sem_par += 1
                continue
            onum = str(orig_nf[0] or "").lstrip("0")
            ocnpj = re.sub(r"\D", "", orig_nf[1] or "")
            orig = conn.execute(
                "SELECT e.chave_cte FROM entrega_nfs n JOIN entregas e ON e.chave_cte = n.chave_cte "
                "WHERE n.numero_nf = ? AND n.cnpj_emissor = ? AND e.chave_cte <> ? "
                "AND e.chave_cte NOT LIKE 'ROM:%' LIMIT 1", (onum, ocnpj, dev["chave_cte"])).fetchone()
            if not orig:
                sem_par += 1
                continue
            conn.execute(
                "UPDATE entregas SET tipo_cte = 'DEVOLUCAO', devolucao_de = ?, atualizado_em = ? WHERE chave_cte = ?",
                (orig["chave_cte"], now, dev["chave_cte"]))
            conn.execute("UPDATE entregas SET devolvida = 1, atualizado_em = ? WHERE chave_cte = ?",
                         (now, orig["chave_cte"]))
            vinculadas += 1
        conn.commit()
    finally:
        conn.close()
    return {"vinculadas": vinculadas, "sem_par": sem_par}


def buscar_valor_ida(numero_nf: str, serie: str, cnpj_emissor: str, excluir_chave_cte: str = "") -> Optional[Dict]:
    """Acha o CT-e de IDA original de uma NF (o mais antigo já auditado com
    valor_cobrado, que não seja o próprio CT-e de devolução) — usado pela
    auditoria para conferir devolução contra 100% do valor da ida (ver
    app.auditor._audit_devolucao). numero_nf/serie seguem a mesma normalização
    gravada em entrega_nfs (sem zeros à esquerda / "0" default)."""
    numero = (numero_nf or "").lstrip("0") or "0"
    serie_n = (serie or "").strip() or "0"
    cnpj = re.sub(r"\D", "", cnpj_emissor or "")
    if not numero or not cnpj:
        return None
    conn = _get_conn()
    try:
        row = conn.execute("""
            SELECT h.chave_cte, h.numero_cte, h.valor_cobrado, h.data_emissao
            FROM entrega_nfs n
            JOIN audit_history h ON h.chave_cte = n.chave_cte
            WHERE n.numero_nf = ? AND n.serie = ? AND n.cnpj_emissor = ?
              AND n.chave_cte != ? AND h.valor_cobrado IS NOT NULL
            ORDER BY h.data_emissao ASC
            LIMIT 1
        """, (numero, serie_n, cnpj, excluir_chave_cte or "")).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def buscar_valor_ida_por_cliente(
    cnpj_cliente: str, antes_de: Optional[str] = None, excluir_chave_cte: str = "",
) -> Optional[Dict]:
    """Fallback de buscar_valor_ida: quando a NF não bate (ida nunca foi
    auditada com essa NF, ou a devolução não carrega chave de NF-e), tenta
    achar a ida pelo CNPJ/CPF do CLIENTE — o remetente da devolução é quem
    recebeu a ida (mesma pessoa, direção invertida). Pega o CT-e "normal" mais
    recente pra esse cliente ANTES da data da devolução (mais provável de ser
    a mercadoria que está voltando). Só entra CT-e com preço real da tabela
    (status OK/DIVERGENTE, tipo_operacao nulo — não casa devolução com outra
    devolução/reentrega). Resultado é aproximado — sempre fica PENDENTE pra
    confirmação humana (ver app.auditor._audit_operacao_especial)."""
    cnpj = re.sub(r"\D", "", cnpj_cliente or "")
    if not cnpj:
        return None
    conn = _get_conn()
    try:
        q = """
            SELECT chave_cte, numero_cte, valor_cobrado, data_emissao
            FROM audit_history
            WHERE destinatario_cnpj = ? AND status IN ('OK', 'DIVERGENTE')
              AND tipo_operacao IS NULL AND valor_cobrado IS NOT NULL
              AND chave_cte != ?
        """
        params: list = [cnpj, excluir_chave_cte or ""]
        if antes_de:
            q += " AND (data_emissao IS NULL OR data_emissao <= ?)"
            params.append(antes_de)
        q += " ORDER BY data_emissao DESC LIMIT 1"
        row = conn.execute(q, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def buscar_valor_ida_manual(numero_nf: str, excluir_chave_cte: str = "") -> Optional[Dict]:
    """Vínculo manual: usuário digita o número da NF de origem quando a
    auditoria automática não achou (nem por NF exata nem pelo CNPJ do
    cliente — ver app.auditor._audit_operacao_especial). Menos rígido que
    buscar_valor_ida: não exige série nem CNPJ emissor exatos (o usuário só
    tem o número em mãos, então casa só pelo número, sempre CNPJ NOSSO —
    a ida de uma devolução/reentrega é sempre uma NF que nós emitimos)."""
    numero = (numero_nf or "").strip()
    numero = re.sub(r"\D", "", numero).lstrip("0") or (numero and "0") or ""
    if not numero:
        return None
    conn = _get_conn()
    try:
        row = conn.execute("""
            SELECT h.chave_cte, h.numero_cte, h.valor_cobrado, h.data_emissao
            FROM entrega_nfs n
            JOIN audit_history h ON h.chave_cte = n.chave_cte
            WHERE n.numero_nf = ? AND n.cnpj_emissor = ?
              AND n.chave_cte != ? AND h.valor_cobrado IS NOT NULL
            ORDER BY h.data_emissao DESC
            LIMIT 1
        """, (numero, "66957537000152", excluir_chave_cte or "")).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def vincular_devolucao(chave_devolucao: str, chave_original: str) -> Dict:
    """Vincula um CT-e de DEVOLUÇÃO (retorno) ao CT-e ORIGINAL da mesma mercadoria
    (ex.: cliente cancelou e foi emitido o CT-e de devolução). Efeito: o original
    passa a 'Devolvida' (fora do OTD) e o de retorno recebe o rótulo 'CT-e de
    devolução' (fora do OTD), apontando para o original."""
    if not chave_devolucao or not chave_original:
        raise ValueError("Informe os dois CT-es (devolução e original).")
    if chave_devolucao == chave_original:
        raise ValueError("O CT-e de devolução e o original não podem ser o mesmo.")
    conn = _get_conn()
    try:
        existentes = {r["chave_cte"] for r in conn.execute(
            "SELECT chave_cte FROM entregas WHERE chave_cte IN (?,?)",
            (chave_devolucao, chave_original)).fetchall()}
        if chave_devolucao not in existentes:
            raise ValueError("CT-e de devolução não encontrado na Torre.")
        if chave_original not in existentes:
            raise ValueError("CT-e original não encontrado na Torre.")
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE entregas SET devolucao_de = ?, atualizado_em = ? WHERE chave_cte = ?",
                     (chave_original, now, chave_devolucao))
        conn.execute("UPDATE entregas SET devolvida = 1, atualizado_em = ? WHERE chave_cte = ?",
                     (now, chave_original))
        conn.commit()
    finally:
        conn.close()
    return {"devolucao": chave_devolucao, "original": chave_original}


def vincular_devolucao_por_nf(chave_devolucao: str, numero_nf_origem: str,
                              cnpj_emissor: Optional[str] = None) -> Dict:
    """Vincula um CT-e de DEVOLUÇÃO à sua origem informando a NF de ORIGEM (mais
    natural que o nº do CT-e — a devolução refere a NF, não o CT-e). Acha a entrega
    que carrega essa NF e amarra as duas. Erro claro se não achar ou se houver mais
    de um CT-e com a mesma NF (aí desambigua pelo nº do CT-e)."""
    numero = str(numero_nf_origem or "").lstrip("0")
    if not numero:
        raise ValueError("Informe o número da NF de origem.")
    cnpj = re.sub(r"\D", "", cnpj_emissor or "")
    conn = _get_conn()
    try:
        where = ("n.numero_nf = ? AND e.chave_cte <> ? AND e.chave_cte NOT LIKE 'ROM:%' "
                 "AND e.devolucao_de IS NULL AND (e.tipo_cte IS NULL OR e.tipo_cte <> 'DEVOLUCAO')")
        params = [numero, chave_devolucao]
        if cnpj:
            where += " AND n.cnpj_emissor = ?"
            params.append(cnpj)
        rows = conn.execute(
            "SELECT DISTINCT e.chave_cte, e.numero_cte FROM entrega_nfs n "
            "JOIN entregas e ON e.chave_cte = n.chave_cte WHERE " + where, params
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise ValueError(f"Nenhum CT-e de origem encontrado na Torre para a NF {numero}.")
    if len(rows) > 1:
        ctes = ", ".join(str(r["numero_cte"]) for r in rows)
        raise ValueError(f"Há mais de um CT-e com a NF {numero} ({ctes}) — desambigue pelo nº do CT-e.")
    return vincular_devolucao(chave_devolucao, rows[0]["chave_cte"])


def desvincular_devolucao(chave_devolucao: str) -> Dict:
    """Desfaz o vínculo de devolução: o CT-e volta a entrega normal e o original
    deixa de ser 'Devolvida' (se não houver outra devolução ligada a ele)."""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT devolucao_de FROM entregas WHERE chave_cte = ?",
                           (chave_devolucao,)).fetchone()
        original = row["devolucao_de"] if row else None
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE entregas SET devolucao_de = NULL, atualizado_em = ? WHERE chave_cte = ?",
                     (now, chave_devolucao))
        if original:
            resta = conn.execute(
                "SELECT COUNT(*) FROM entregas WHERE devolucao_de = ?", (original,)).fetchone()[0]
            if not resta:
                conn.execute("UPDATE entregas SET devolvida = 0, atualizado_em = ? WHERE chave_cte = ?",
                             (now, original))
        conn.commit()
    finally:
        conn.close()
    return {"devolucao": chave_devolucao, "original": original}


def kpis(
    mes: Optional[str] = None,
    transportadora: Optional[str] = None,
    uf: Optional[str] = None,
    entregas: Optional[List[Dict]] = None,
) -> Dict:
    """OTD geral e por transportadora. `entregas` permite calcular sobre uma
    lista já filtrada (usado na exportação, para o resumo bater com as linhas)."""
    if entregas is None:
        entregas = query_entregas(transportadora=transportadora, mes=mes, uf=uf, limit=100000)
    hoje = date.today()

    geral = novo_bucket()
    por_transp: Dict[str, Dict] = {}

    for e in entregas:
        classificar_entrega(geral, e)
        b_t = por_transp.setdefault(e["transportadora"] or "Desconhecida", novo_bucket())
        classificar_entrega(b_t, e)
        if e.get("problema"):
            geral["com_problema"] += 1
            b_t["com_problema"] += 1

    def _fin(b, nome=None):
        d = {
            "total": b["total"], "entregues": b["entregues"],
            "avaliadas_otd": avaliadas_otd(b), "entregue_sem_prazo": b["entregue_sem_prazo"],
            "otd_pct": otd_do_bucket(b),
            "otd_ajustado_pct": otd_ajustado_do_bucket(b),
            "otd_potencial_pct": otd_potencial_do_bucket(b),
            "em_transito": b["em_transito"], "atrasadas_abertas": b["atrasadas_abertas"],
            "devolvidas": b["devolvidas"], "sem_prazo": b["sem_prazo"],
            "com_problema": b["com_problema"],
            "atraso_medio_dias": atraso_medio_do_bucket(b),
        }
        if nome is not None:
            d["transportadora"] = nome
        return d

    conn = _get_conn()
    pendentes = conn.execute("SELECT COUNT(*) AS c FROM ocorrencias WHERE chave_cte IS NULL").fetchone()["c"]
    conn.close()

    return {
        "geral": _fin(geral),
        "ocorrencias_sem_vinculo": pendentes,
        "hoje": hoje.isoformat(),
        "por_transportadora": sorted(
            (_fin(b, nome) for nome, b in por_transp.items()),
            # pior OTD primeiro; quem ainda não tem OTD (nada entregue) vai para o fim
            key=lambda x: (x["otd_pct"] is None, x["otd_pct"] if x["otd_pct"] is not None else 0, -x["total"]),
        ),
    }


def ocorrencias_sem_vinculo(limit: int = 200) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute("""
        SELECT * FROM ocorrencias WHERE chave_cte IS NULL
        ORDER BY data_ocorrencia DESC, id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ocorrencias_por_chave() -> Dict[str, List[Dict]]:
    """Ocorrências vinculadas a um CTe, agrupadas por chave_cte — insumo do Mapa
    de Ocorrências (app/ocorrencias.py). Só as com vínculo (chave_cte não nula);
    as órfãs seguem em ocorrencias_sem_vinculo(). Ordenadas por data/hora."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT chave_cte, codigo, descricao, categoria, data_ocorrencia, hora, numero_nf
        FROM ocorrencias WHERE chave_cte IS NOT NULL
        ORDER BY data_ocorrencia, hora, id
    """).fetchall()
    conn.close()
    por_chave: Dict[str, List[Dict]] = {}
    for r in rows:
        por_chave.setdefault(r["chave_cte"], []).append(dict(r))
    return por_chave


def total_sem_vinculo() -> int:
    conn = _get_conn()
    n = conn.execute("SELECT COUNT(*) FROM ocorrencias WHERE chave_cte IS NULL").fetchone()[0]
    conn.close()
    return int(n)


def list_meses() -> List[str]:
    conn = _get_conn()
    rows = conn.execute("""
        SELECT DISTINCT substr(data_emissao, 1, 7) AS mes FROM entregas
        WHERE data_emissao IS NOT NULL ORDER BY mes DESC
    """).fetchall()
    conn.close()
    return [r["mes"] for r in rows if r["mes"]]


def list_ufs() -> List[str]:
    """UFs distintas presentes nas entregas (últimos 2 caracteres do destino),
    para alimentar o filtro por estado da Torre."""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT DISTINCT UPPER(SUBSTR(TRIM(destino), -2)) AS uf FROM entregas
        WHERE destino IS NOT NULL AND TRIM(destino) <> '' ORDER BY uf
    """).fetchall()
    conn.close()
    # só siglas de 2 letras (descarta destinos malformados)
    return [r["uf"] for r in rows if r["uf"] and len(r["uf"]) == 2 and r["uf"].isalpha()]


# ─── Exportação de relatórios ────────────────────────────────────────────────

STATUS_LABEL = {
    "ENTREGUE_NO_PRAZO": "Entregue no prazo",
    "ENTREGUE_ATRASADO": "Entregue com atraso",
    "ENTREGUE": "Entregue (sem prazo)",
    "ATRASADO": "Atrasado (sem baixa)",
    "VENCE_HOJE": "Vence hoje",
    "EM_TRANSITO": "Em trânsito",
    "DEVOLVIDA": "Devolvida",
    "DEVOLUCAO": "CT-e de devolução",
    "SEM_PRAZO": "Sem prazo de tabela",
}

_STATUS_FILL = {
    "ENTREGUE_NO_PRAZO": "C6EFCE", "ENTREGUE": "C6EFCE",
    "ENTREGUE_ATRASADO": "FFC7CE", "ATRASADO": "FFC7CE",
    "VENCE_HOJE": "FFEB9C", "DEVOLVIDA": "FFEB9C", "DEVOLUCAO": "E7D8F3",
    "EM_TRANSITO": "DDEBF7", "SEM_PRAZO": "D9D9D9",
}


def _ocorrencias_das_entregas(chaves: List[str]) -> List[Dict]:
    if not chaves:
        return []
    conn = _get_conn()
    rows: List[Dict] = []
    # lotes de 500 para não estourar o limite de parâmetros do SQLite
    for i in range(0, len(chaves), 500):
        lote = chaves[i:i + 500]
        marks = ",".join("?" * len(lote))
        rows.extend(dict(r) for r in conn.execute(f"""
            SELECT o.*, e.numero_cte, e.transportadora
            FROM ocorrencias o JOIN entregas e ON e.chave_cte = o.chave_cte
            WHERE o.chave_cte IN ({marks})
            ORDER BY o.data_ocorrencia, o.hora, o.id
        """, lote).fetchall())
    conn.close()
    return rows


def export_csv(rows: List[Dict]) -> str:
    import csv
    import io
    from app.history import _sanitize_cell

    buf = io.StringIO()
    campos = [
        "data_liberacao", "data_emissao", "numero_cte", "chave_cte", "transportadora", "destino", "uf",
        "total_nfs", "nfs_baixadas", "numeros_nf", "prazo_dias", "data_prevista",
        "data_entrega_real", "entrega_manual", "dias_atraso", "status",
        "ultima_ocorrencia", "ultima_ocorrencia_data",
    ]
    writer = csv.DictWriter(buf, fieldnames=campos, extrasaction="ignore", delimiter=";")
    writer.writeheader()
    for r in rows:
        out = dict(r)
        out["status"] = STATUS_LABEL.get(r["status"], r["status"])
        _uf = (r.get("destino") or "").strip()[-2:].upper()
        out["uf"] = _uf if _uf.isalpha() else ""
        writer.writerow({k: _sanitize_cell(v) for k, v in out.items()})
    return buf.getvalue()


def export_xlsx(rows: List[Dict], kpis_data: Dict, filtros: Optional[Dict] = None) -> bytes:
    """Relatório de entregas formatado: aba Entregas + aba OTD + aba Ocorrências."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from app.history import _sanitize_cell

    header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    def _cabecalho(ws, headers, row):
        for col_idx, label in enumerate(headers, start=1):
            c = ws.cell(row=row, column=col_idx, value=label)
            c.fill = header_fill
            c.font = header_font
            c.alignment = Alignment(horizontal="center")

    def _fmt_data(iso):
        return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso and len(iso) >= 10 else None

    wb = Workbook()

    # ── Aba 1: Entregas ─────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Entregas"
    ws["A1"] = "Relatório de Entregas — Torre de Controle"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    filtros = {k: v for k, v in (filtros or {}).items() if v}
    if filtros:
        ws["A3"] = "Filtros: " + ", ".join(f"{k}={v}" for k, v in filtros.items())

    g = kpis_data["geral"]
    resumo = [
        ("Total de entregas", g["total"]),
        ("Entregues", g["entregues"]),
        ("OTD (base: entregas com prazo)", f"{g['otd_pct']}%" if g["otd_pct"] is not None else "—"),
        ("Entregas avaliadas no OTD", g.get("avaliadas_otd", 0)),
        ("Entregues sem prazo (fora do OTD)", g.get("entregue_sem_prazo", 0)),
        ("Atraso médio (dias)", g["atraso_medio_dias"]),
        ("Em trânsito", g["em_transito"]),
        ("Atrasadas em aberto", g["atrasadas_abertas"]),
        ("Devolvidas", g["devolvidas"]),
        ("Sem prazo de tabela (não entregues)", g["sem_prazo"]),
    ]
    for i, (label, valor) in enumerate(resumo, start=5):
        ws[f"A{i}"] = label
        ws[f"A{i}"].font = Font(bold=True)
        ws[f"B{i}"] = valor

    headers = ["Liberação", "Nº CTe", "Transportadora", "Destino", "UF", "NFs (baixadas/total)",
               "Nº NF", "Prazo (dias úteis)", "Prevista", "Entrega real", "Baixa manual",
               "Atraso (dias)", "Status", "Última ocorrência", "Data ocorrência", "Chave CTe"]
    header_row = 14
    _cabecalho(ws, headers, header_row)

    for row_idx, r in enumerate(rows, start=header_row + 1):
        _uf = (r.get("destino") or "").strip()[-2:].upper()
        valores = [
            _fmt_data(r.get("data_liberacao") or r["data_emissao"]), r["numero_cte"], r["transportadora"], r["destino"],
            _uf if _uf.isalpha() else "",
            f"{r['nfs_baixadas']}/{r['total_nfs']}", r.get("numeros_nf"), r["prazo_dias"],
            _fmt_data(r["data_prevista"]), _fmt_data(r["data_entrega_real"]),
            "Sim" if r["entrega_manual"] else "",
            r["dias_atraso"], STATUS_LABEL.get(r["status"], r["status"]),
            r["ultima_ocorrencia"], _fmt_data(r.get("ultima_ocorrencia_data")), r["chave_cte"],
        ]
        for col_idx, val in enumerate(valores, start=1):
            ws.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
        fill = _STATUS_FILL.get(r["status"])
        if fill:
            pf = PatternFill(start_color=fill, end_color=fill, fill_type="solid")
            for col_idx in range(1, len(headers) + 1):
                ws.cell(row=row_idx, column=col_idx).fill = pf

    for col_idx, w in enumerate([12, 10, 20, 20, 6, 16, 18, 14, 12, 12, 10, 11, 20, 34, 14, 46], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = w
    ws.freeze_panes = f"A{header_row + 1}"

    # ── Aba 2: OTD por transportadora ───────────────────────────────────
    ws2 = wb.create_sheet("OTD por transportadora")
    ws2["A1"] = "OTD por transportadora"
    ws2["A1"].font = Font(bold=True, size=13)
    headers2 = ["Transportadora", "Entregas", "Entregues", "OTD %", "Atraso médio (dias)",
                "Em trânsito", "Atrasadas em aberto", "Devolvidas", "Sem prazo"]
    _cabecalho(ws2, headers2, 3)
    for row_idx, t in enumerate(kpis_data["por_transportadora"], start=4):
        vals = [t["transportadora"], t["total"], t["entregues"],
                t["otd_pct"] if t["otd_pct"] is not None else "—", t["atraso_medio_dias"],
                t["em_transito"], t["atrasadas_abertas"], t["devolvidas"], t["sem_prazo"]]
        for col_idx, val in enumerate(vals, start=1):
            ws2.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
        otd = t["otd_pct"]
        if otd is not None:
            cor = "C6EFCE" if otd >= 95 else ("FFEB9C" if otd >= 85 else "FFC7CE")
            ws2.cell(row=row_idx, column=4).fill = PatternFill(start_color=cor, end_color=cor, fill_type="solid")
    for col_idx, w in enumerate([26, 10, 10, 8, 17, 11, 17, 11, 10], start=1):
        ws2.column_dimensions[get_column_letter(col_idx)].width = w
    ws2.freeze_panes = "A4"

    # ── Aba 3: Ocorrências das entregas filtradas ───────────────────────
    ws3 = wb.create_sheet("Ocorrências")
    ws3["A1"] = "Ocorrências EDI das entregas do relatório"
    ws3["A1"].font = Font(bold=True, size=13)
    headers3 = ["Data", "Hora", "Nº CTe", "Transportadora", "NF", "Série",
                "Código", "Descrição", "Categoria", "Complemento", "Arquivo EDI"]
    _cabecalho(ws3, headers3, 3)
    ocorrencias = _ocorrencias_das_entregas([r["chave_cte"] for r in rows])
    for row_idx, o in enumerate(ocorrencias, start=4):
        vals = [_fmt_data(o["data_ocorrencia"]), o["hora"], o["numero_cte"], o["transportadora"],
                o["numero_nf"], o["serie"], o["codigo"], o["descricao"], o["categoria"],
                o["texto"], o["arquivo_origem"]]
        for col_idx, val in enumerate(vals, start=1):
            ws3.cell(row=row_idx, column=col_idx, value=_sanitize_cell(val))
    for col_idx, w in enumerate([12, 7, 10, 20, 10, 7, 8, 40, 18, 30, 26], start=1):
        ws3.column_dimensions[get_column_letter(col_idx)].width = w
    ws3.freeze_panes = "A4"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ─── Integração com o Romaneio de Expedição ──────────────────────────────────
# Quando um romaneio é "liberado" (transportadora levou), suas NFs entram na
# Torre ANTES do CTe, como entregas provisórias com chave sintética "ROM:<id>:<nf>".
# Assim o OCOREN já baixa a entrega por (CNPJ emissor + nº NF) mesmo sem CTe.
# Ao auditar o CTe real que cobre a NF, o provisório é removido (ver
# _remover_provisorios_de_nf, chamado por upsert_from_audit) — sem contagem dupla.

_PREFIXO_ROM = "ROM:"


def _tem_cte_real(conn: sqlite3.Connection, cnpj_emissor: str, numero_nf: str) -> bool:
    """True se a NF já tem um CT-e REAL na Torre (entrega não-provisória). Usado
    para não recriar provisório de romaneio quando o CTe já chegou — senão
    re-liberar um romaneio duplicaria a NF na Torre com data de liberação de hoje."""
    row = conn.execute(
        "SELECT 1 FROM entrega_nfs n JOIN entregas e ON e.chave_cte = n.chave_cte "
        "WHERE n.numero_nf = ? AND n.cnpj_emissor = ? AND e.chave_cte NOT LIKE ? LIMIT 1",
        (numero_nf, re.sub(r"\D", "", cnpj_emissor or ""), f"{_PREFIXO_ROM}%"),
    ).fetchone()
    return row is not None


def registrar_romaneio(romaneio_id: int, numero: str, transportadora: str,
                       transportadora_cnpj: str, data_expedicao: Optional[str],
                       itens: List[Dict], data_liberacao: Optional[str] = None) -> int:
    """Cria/atualiza entregas provisórias na Torre para as NFs de um romaneio
    liberado. `data_liberacao` (quando a carga foi liberada) é a base do prazo e o
    que a Torre mostra na coluna "Liberação"; sem ela, cai para a data de expedição.
    `itens`: dicts com chave, nf_numero, nf_serie, emitente_cnpj, dest_municipio,
    dest_uf, prazo_dias, data_prevista. Retorna nº de NFs."""
    now = datetime.now().isoformat(timespec="seconds")
    emissao = (data_expedicao or "")[:10] or None
    liberacao = (data_liberacao or "")[:10] or emissao
    n = 0
    conn = _get_conn()
    try:
        for it in itens:
            numero_nf = str(it.get("nf_numero") or "").lstrip("0")
            if not numero_nf:
                continue
            # Se o CT-e real já chegou, não recria provisório (e limpa um provisório
            # antigo, se houver): o CTe real já rastreia a NF com a liberação correta.
            if _tem_cte_real(conn, it.get("emitente_cnpj") or "", numero_nf):
                _remover_provisorios_de_nf(conn, it.get("emitente_cnpj") or "", numero_nf)
                continue
            chave_cte = f"{_PREFIXO_ROM}{romaneio_id}:{numero_nf}"
            destino = (it.get("dest_municipio") or "")
            if it.get("dest_uf"):
                destino = f"{destino}/{it['dest_uf']}" if destino else it["dest_uf"]
            serie = str(it.get("nf_serie") or "0").lstrip("0") or "0"
            existing = conn.execute(
                "SELECT data_entrega_real, entrega_manual, devolvida, criado_em FROM entregas WHERE chave_cte=?",
                (chave_cte,),
            ).fetchone()
            # Previsão sobre a liberação (base correta); sem prazo, usa a do item.
            prev = it.get("data_prevista")
            if liberacao and it.get("prazo_dias"):
                try:
                    prev = somar_dias_uteis(
                        date.fromisoformat(liberacao), int(it["prazo_dias"])
                    ).isoformat()
                except (ValueError, TypeError):
                    pass
            conn.execute("""
                INSERT INTO entregas (
                    chave_cte, numero_cte, arquivo, transportadora, transportadora_cnpj,
                    destino, data_emissao, data_liberacao, prazo_dias, data_prevista,
                    data_entrega_real, entrega_manual, devolvida, criado_em, atualizado_em
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chave_cte) DO UPDATE SET
                    transportadora=excluded.transportadora, transportadora_cnpj=excluded.transportadora_cnpj,
                    destino=excluded.destino, data_emissao=excluded.data_emissao,
                    data_liberacao=excluded.data_liberacao,
                    prazo_dias=COALESCE(excluded.prazo_dias, entregas.prazo_dias),
                    data_prevista=COALESCE(excluded.data_prevista, entregas.data_prevista),
                    atualizado_em=excluded.atualizado_em
            """, (
                chave_cte, f"Romaneio {numero}", f"romaneio:{romaneio_id}",
                transportadora, re.sub(r"\D", "", transportadora_cnpj or ""),
                destino, emissao, liberacao, it.get("prazo_dias"), prev,
                existing["data_entrega_real"] if existing else None,
                existing["entrega_manual"] if existing else 0,
                existing["devolvida"] if existing else 0,
                existing["criado_em"] if existing else now, now,
            ))
            conn.execute("""
                INSERT OR IGNORE INTO entrega_nfs (chave_cte, chave_nfe, cnpj_emissor, serie, numero_nf, data_entrega)
                VALUES (?,?,?,?,?,NULL)
            """, (chave_cte, it.get("chave") or "", re.sub(r"\D", "", it.get("emitente_cnpj") or ""), serie, numero_nf))
            _relink_pendentes(conn, chave_cte)
            _recomputar_baixa(conn, chave_cte)
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def remover_romaneio(romaneio_id: int) -> int:
    """Remove as entregas provisórias de um romaneio (ex.: ao cancelar/excluir)."""
    conn = _get_conn()
    like = f"{_PREFIXO_ROM}{romaneio_id}:%"
    conn.execute("DELETE FROM entrega_nfs WHERE chave_cte LIKE ?", (like,))
    cur = conn.execute("DELETE FROM entregas WHERE chave_cte LIKE ?", (like,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def remover_provisorio_nf(cnpj_emissor: str, numero_nf: str) -> None:
    """Remove o provisório ROM: de UMA NF (ex.: ao mover/remover a NF de um
    romaneio). Abre a própria conexão."""
    conn = _get_conn()
    try:
        _remover_provisorios_de_nf(conn, cnpj_emissor, numero_nf)
        conn.commit()
    finally:
        conn.close()


def _remover_provisorios_de_nf(conn: sqlite3.Connection, cnpj_emissor: str, numero_nf: str) -> None:
    """Quando um CTe real passa a cobrir a NF, apaga o provisório ROM: dela para
    não contar a entrega duas vezes no OTD."""
    cnpj = re.sub(r"\D", "", cnpj_emissor or "")
    numero = str(numero_nf or "").lstrip("0")
    if not numero:
        return
    rows = conn.execute(
        "SELECT chave_cte FROM entrega_nfs WHERE chave_cte LIKE ? AND cnpj_emissor=? AND numero_nf=?",
        (f"{_PREFIXO_ROM}%", cnpj, numero),
    ).fetchall()
    for r in rows:
        # Ocorrências ligadas ao provisório voltam a "pendentes" (chave_cte NULL)
        # para serem religadas ao CTe real por _relink_pendentes — sem isto elas
        # ficariam órfãs (apontando um provisório apagado) e sumiriam da tela.
        conn.execute("UPDATE ocorrencias SET chave_cte = NULL WHERE chave_cte=?", (r["chave_cte"],))
        conn.execute("DELETE FROM entrega_nfs WHERE chave_cte=?", (r["chave_cte"],))
        conn.execute("DELETE FROM entregas WHERE chave_cte=?", (r["chave_cte"],))


def ctes_de_nfs(cnpj_emissor: str, numeros: List[str]) -> Dict[str, str]:
    """{numero_nf(sem zeros): numero_cte} para as NFs que um CTe REAL referencia
    (via entrega_nfs, populado pela auditoria de CTe). Exclui os provisórios do
    romaneio (chave 'ROM:%'). Vínculo exato — não é heurística de valor."""
    cnpj = re.sub(r"\D", "", cnpj_emissor or "")
    nums = [str(x or "").lstrip("0") for x in numeros if str(x or "").lstrip("0")]
    if not cnpj or not nums:
        return {}
    marks = ",".join("?" * len(nums))
    conn = _get_conn()
    out: Dict[str, str] = {}
    for numero, numero_cte in conn.execute(
        f"""SELECT n.numero_nf, e.numero_cte
            FROM entrega_nfs n JOIN entregas e ON e.chave_cte = n.chave_cte
            WHERE n.cnpj_emissor=? AND n.numero_nf IN ({marks})
              AND n.chave_cte NOT LIKE 'ROM:%'
            ORDER BY e.data_emissao""", [cnpj, *nums]).fetchall():
        if numero not in out:
            out[numero] = numero_cte or "?"
    conn.close()
    return out


def datas_entrega_de_nfs(cnpj_emissor: str, numeros: List[str]) -> Dict[str, str]:
    """{numero_nf(sem zeros): data_entrega} para as NFs entregues. Considera TODAS
    as formas de baixa: (1) entrega_nfs.data_entrega (baixa por OCOREN por NF),
    (2) entregas.data_entrega_real da entrega que contém a NF — cobre a **baixa
    manual** (entrega_manual=1) e a baixa consolidada — e (3) ocorrências
    categoria ENTREGUE. Serve ao encerramento do romaneio."""
    cnpj = re.sub(r"\D", "", cnpj_emissor or "")
    nums = [str(x or "").lstrip("0") for x in numeros if str(x or "").lstrip("0")]
    if not cnpj or not nums:
        return {}
    marks = ",".join("?" * len(nums))
    conn = _get_conn()
    out: Dict[str, str] = {}
    # (1) baixa por NF no entrega_nfs
    for numero, data in conn.execute(
        f"""SELECT numero_nf, MAX(data_entrega) FROM entrega_nfs
            WHERE cnpj_emissor=? AND numero_nf IN ({marks}) AND data_entrega IS NOT NULL
            GROUP BY numero_nf""", [cnpj, *nums]).fetchall():
        if data:
            out[numero] = data
    # (2) baixa da entrega (manual ou consolidada) que contém a NF
    for numero, data in conn.execute(
        f"""SELECT n.numero_nf, MAX(e.data_entrega_real)
            FROM entrega_nfs n JOIN entregas e ON e.chave_cte = n.chave_cte
            WHERE n.cnpj_emissor=? AND n.numero_nf IN ({marks}) AND e.data_entrega_real IS NOT NULL
            GROUP BY n.numero_nf""", [cnpj, *nums]).fetchall():
        if data and numero not in out:
            out[numero] = data
    # (3) reforço: ocorrências de entrega
    for numero, data in conn.execute(
        f"""SELECT numero_nf, MAX(data_ocorrencia) FROM ocorrencias
            WHERE cnpj_emissor=? AND numero_nf IN ({marks}) AND categoria='ENTREGUE'
            GROUP BY numero_nf""", [cnpj, *nums]).fetchall():
        if data and numero not in out:
            out[numero] = data
    conn.close()
    return out
