"""
Carteiro de EDI — lê a caixa de e-mail (IMAP) e baixa os anexos para as pastas
de entrada do sistema, roteando por tipo. Mesmo padrão do auto-sync do Olist.

Roteamento:
  CTe (XML)  -> cte_xml/entrada/   (Auditoria de CTe)
  OCOREN     -> edi/entrada/       (Torre de Controle)
  DOCCOB     -> doccob/entrada/    (Conferência de Fatura — importar na tela)
  NOTFIS     -> IGNORADO (sistema não processa NOTFIS)

Classificação = NOME do anexo + CONTEÚDO (registros PROCEDA). DOCCOB e NOTFIS
compartilham os registros 310/311, então quando o conteúdo é ambíguo, é o NOME
que decide; sem pista no nome, o anexo é deixado de lado (não roteado) por segurança.

Configuração no .env (a SENHA é uma "senha de app", nunca a senha real; gitignored):
  EMAIL_IMAP_HOST, EMAIL_IMAP_PORTA (993), EMAIL_IMAP_SSL (1),
  EMAIL_USUARIO, EMAIL_SENHA, EMAIL_PASTA (INBOX),
  EMAIL_AUTO (1), EMAIL_INTERVALO_MIN (10)
"""
from __future__ import annotations

import email
import imaplib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from email.header import decode_header
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("email_ingest")

_ROOT = Path(__file__).parent.parent
_ENV_FILE = _ROOT / ".env"
_STATE_FILE = Path(os.getenv("EMAIL_STATE", str(_ROOT / "tables" / "email_ingest.json")))


def _load_env() -> None:
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
    except Exception as e:
        logger.warning(f"Falha ao ler .env: {e}")


_load_env()

# Pastas de entrada (mesmos caminhos que o main.py usa).
CTE_ENTRADA = Path(os.getenv("CTE_DIR", str(_ROOT / "cte_xml"))) / "entrada"
EDI_ENTRADA = Path(os.getenv("EDI_DIR", str(_ROOT / "edi"))) / "entrada"
DOCCOB_ENTRADA = Path(os.getenv("DOCCOB_DIR", str(_ROOT / "doccob"))) / "entrada"

_DESTINO = {"cte": CTE_ENTRADA, "ocoren": EDI_ENTRADA, "doccob": DOCCOB_ENTRADA}

_OCOREN_EXT = {".txt", ".oco", ".edi", ".ocr", ".pro"}
EDI_PROCESSADOS = Path(os.getenv("EDI_DIR", str(_ROOT / "edi"))) / "processados"


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
def _get(chave: str, default: str = "") -> str:
    return (os.getenv(chave, default) or "").strip()


def configurado() -> bool:
    return bool(_get("EMAIL_IMAP_HOST") and _get("EMAIL_USUARIO") and _get("EMAIL_SENHA"))


def auto_ligado() -> bool:
    return _get("EMAIL_AUTO", "1").lower() not in ("0", "false", "nao", "não", "")


def intervalo_min() -> int:
    try:
        return max(2, int(_get("EMAIL_INTERVALO_MIN", "10")))
    except ValueError:
        return 10


# --------------------------------------------------------------------------- #
# Classificação do anexo
# --------------------------------------------------------------------------- #
def classificar(nome: str, conteudo: bytes) -> str:
    """Retorna: 'cte' | 'ocoren' | 'doccob' | 'notfis' | 'ambiguo' | 'desconhecido'."""
    n = (nome or "").lower()
    ini = conteudo[:4000]

    # 1) XML -> CTe (o e-mail de EDI recebe o CTe em XML)
    if (n.endswith(".xml") or ini.lstrip()[:6] == b"<?xml"
            or b"<CTe" in ini or b"infCte" in conteudo[:6000]):
        return "cte"

    # 2) pelo NOME (é o que separa DOCCOB de NOTFIS com segurança)
    if "ocoren" in n or "ocorren" in n:
        return "ocoren"
    if "doccob" in n:
        return "doccob"
    if "notfis" in n:
        return "notfis"
    if "cte" in n:
        return "cte"

    # 3) pelo CONTEÚDO (registros PROCEDA nos 3 primeiros caracteres de cada linha)
    txt = ini.decode("latin-1", "ignore")
    recs = {ln[:3] for ln in txt.splitlines() if len(ln) >= 3}
    if recs & {"540", "542", "340", "342", "541", "341"}:
        return "ocoren"
    if recs & {"310", "311", "320", "321"}:
        return "ambiguo"  # DOCCOB e NOTFIS usam os mesmos registros — precisa do nome
    return "desconhecido"


def _nome_seguro(nome: str) -> str:
    base = os.path.basename(nome or "anexo.bin")
    limpo = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return limpo or "anexo.bin"


def _decodar_nome(nome: str) -> str:
    try:
        partes = decode_header(nome)
        return "".join(
            (b.decode(enc or "latin-1", "ignore") if isinstance(b, bytes) else b)
            for b, enc in partes
        )
    except Exception:
        return nome


# --------------------------------------------------------------------------- #
# Encadeamento: processa os OCOREN baixados (parse + importa + move)
# --------------------------------------------------------------------------- #
def processar_ocoren_entrada() -> Dict:
    """Processa os OCOREN que caíram em edi/entrada — mesma lógica do 'escanear
    pasta' da Torre. A trava de romaneio liberado é aplicada dentro do import."""
    from app import ocoren_parser, tracking
    try:
        from app import romaneio as romaneio_mod
    except Exception:
        romaneio_mod = None

    novas = dup = rej = erros = 0
    houve = False
    if EDI_ENTRADA.exists():
        for p in sorted(EDI_ENTRADA.iterdir()):
            if not (p.is_file() and p.suffix.lower() in _OCOREN_EXT):
                continue
            try:
                parsed = ocoren_parser.parse_ocoren_bytes(p.read_bytes(), filename=p.name)
                rep = tracking.import_ocoren(parsed)
                novas += rep.get("ocorrencias_novas", 0)
                dup += rep.get("ocorrencias_duplicadas", 0)
                rej += rep.get("rejeitadas_sem_romaneio", 0)
                houve = True
                EDI_PROCESSADOS.mkdir(parents=True, exist_ok=True)
                dest = EDI_PROCESSADOS / p.name
                if dest.exists():
                    dest = EDI_PROCESSADOS / f"{p.stem}_{int(time.time())}{p.suffix}"
                p.replace(dest)  # só sai da entrada quem processou sem erro
            except Exception as ex:
                erros += 1
                logger.warning(f"OCOREN {p.name} não processado (fica na entrada): {ex}")
    if houve and romaneio_mod is not None:
        try:
            romaneio_mod.reconciliar_encerrados()
        except Exception:
            pass
    return {"ocorrencias_novas": novas, "duplicadas": dup,
            "rejeitadas_sem_romaneio": rej, "erros": erros}


# --------------------------------------------------------------------------- #
# Leitura da caixa
# --------------------------------------------------------------------------- #
def ler_email() -> Dict:
    if not configurado():
        return {"ok": False, "erro": "E-mail não configurado. Preencha os campos EMAIL_* no arquivo .env."}

    host = _get("EMAIL_IMAP_HOST")
    porta = int(_get("EMAIL_IMAP_PORTA", "993") or "993")
    usa_ssl = _get("EMAIL_IMAP_SSL", "1").lower() not in ("0", "false", "")
    usuario = _get("EMAIL_USUARIO")
    senha = _get("EMAIL_SENHA")
    pasta = _get("EMAIL_PASTA", "INBOX")

    inicio = time.time()
    resumo = {"emails": 0, "cte": 0, "ocoren": 0, "doccob": 0, "notfis": 0, "ignorados": 0}
    detalhes = []
    try:
        M = imaplib.IMAP4_SSL(host, porta) if usa_ssl else imaplib.IMAP4(host, porta)
        try:
            M.login(usuario, senha)
            M.select(pasta)
            typ, dados = M.search(None, "UNSEEN")
            ids = dados[0].split() if (dados and dados[0]) else []
            for eid in ids:
                typ, msgdata = M.fetch(eid, "(RFC822)")
                if typ != "OK" or not msgdata or not msgdata[0]:
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                resumo["emails"] += 1
                for part in msg.walk():
                    if part.get_content_maintype() == "multipart":
                        continue
                    nome = part.get_filename()
                    if not nome:
                        continue
                    nome = _decodar_nome(nome)
                    conteudo = part.get_payload(decode=True) or b""
                    if not conteudo:
                        continue
                    tipo = classificar(nome, conteudo)
                    dest = _DESTINO.get(tipo)
                    if dest is None:
                        resumo["notfis" if tipo == "notfis" else "ignorados"] += 1
                        if tipo in ("ambiguo", "desconhecido"):
                            detalhes.append({"arquivo": nome, "status": tipo})
                        continue
                    dest.mkdir(parents=True, exist_ok=True)
                    (dest / _nome_seguro(nome)).write_bytes(conteudo)
                    resumo[tipo] += 1
                M.store(eid, "+FLAGS", "\\Seen")  # marca como lido p/ não repetir
        finally:
            try:
                M.logout()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Carteiro EDI falhou: {e}")
        return {"ok": False, "erro": f"Falha ao ler o e-mail: {e}"}

    # Encadeamento: assim que baixa OCOREN, já processa (Torre) respeitando a trava.
    if resumo["ocoren"]:
        try:
            resumo["processamento_ocoren"] = processar_ocoren_entrada()
        except Exception as ex:
            logger.error(f"Falha ao processar OCOREN baixados: {ex}")

    resumo["baixados"] = resumo["cte"] + resumo["ocoren"] + resumo["doccob"]
    resumo["segundos"] = round(time.time() - inicio, 1)
    estado = {"ultima_leitura": datetime.now().isoformat(timespec="seconds"), "ultimo_resultado": resumo}
    _salvar_estado(estado)
    return {"ok": True, **resumo, "detalhe": detalhes}


def _salvar_estado(estado: Dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Falha ao salvar estado do carteiro: {e}")


def _carregar_estado() -> Dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def status() -> Dict:
    estado = _carregar_estado()
    return {
        "configurado": configurado(),
        "auto": auto_ligado(),
        "intervalo_min": intervalo_min(),
        "host": _get("EMAIL_IMAP_HOST"),
        "usuario": _get("EMAIL_USUARIO"),
        "pasta": _get("EMAIL_PASTA", "INBOX"),
        "ultima_leitura": estado.get("ultima_leitura"),
        "ultimo_resultado": estado.get("ultimo_resultado"),
    }


# --------------------------------------------------------------------------- #
# Auto (poller em segundo plano)
# --------------------------------------------------------------------------- #
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop() -> None:
    _stop.wait(25)
    while not _stop.is_set():
        try:
            if configurado() and auto_ligado():
                res = ler_email()
                if res.get("ok") and res.get("baixados"):
                    logger.info(f"Carteiro EDI: {res['baixados']} anexo(s) baixado(s) do e-mail.")
        except Exception as e:
            logger.error(f"Carteiro EDI auto falhou: {e}")
        _stop.wait(intervalo_min() * 60)


def iniciar_auto() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    if not configurado():
        logger.info("Carteiro EDI: e-mail não configurado — auto desligado. Preencha EMAIL_* no .env.")
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="email-ingest", daemon=True)
    _thread.start()
    logger.info(f"Carteiro EDI: auto ligado (a cada {intervalo_min()} min).")


def parar_auto() -> None:
    _stop.set()
