"""
Freight Gateway — padrão Intelipost
Suporta tabelas reais de transportadoras (preços por UF/Região + prazos por CEP/cidade)
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.loaders import load_precos, load_prazos
from app.models import ErrorResponse
from app.cep_resolver import resolve_cep
from app.auditor import audit_many, AuditResult, TOLERANCIA_ABS_PADRAO, TOLERANCIA_PCT_PADRAO, vincular_ida_manual
from app.carrier_mapping import load_mapping, set_mapping, SPOT_SENTINEL
from app import history as audit_history
from app import tracking
from app import relatorio_diario as relatorio_mod
from app import scorecard as scorecard_mod
from app import percent_nf as percent_nf_mod
from app import ocorrencias as ocorrencias_mod
from app import cadastro
from app import cadastro_planilha
from app import comparativo as comparativo_mod
from app import conferencia as conferencia_mod
from app import romaneio as romaneio_mod
from app import olist as olist_mod
from app import simulador as simulador_mod
from app import email_ingest as email_mod
from app import ssw_sync as ssw_sync_mod
from app import jamef_webhook_sync as jamef_webhook_mod
from app import vendas as vendas_mod
from app import coletas as coletas_mod
from app import dashboard as dashboard_mod
from app import auth
from app import access_log
from app.ocoren_parser import OcorenParseError, parse_ocoren_bytes
from app import feriados

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

TABLES_DIR = Path(os.getenv("TABLES_DIR", Path(__file__).parent.parent / "tables"))
PRECOS_FILE = os.getenv("PRECOS_FILE", str(TABLES_DIR / "TABELA_PRECOS_TODAS_TRANSPORTADORAS.xlsx"))
PRAZOS_FILE = os.getenv("PRAZOS_FILE", str(TABLES_DIR / "TABELA_PRAZOS_TODAS_TRANSPORTADORAS.xlsx"))
CTE_DIR = Path(os.getenv("CTE_DIR", Path(__file__).parent.parent / "cte_xml"))
CTE_ENTRADA_DIR = CTE_DIR / "entrada"
CTE_OK_DIR = CTE_DIR / "processados" / "ok"
CTE_NOK_DIR = CTE_DIR / "processados" / "nok"
EDI_DIR = Path(os.getenv("EDI_DIR", Path(__file__).parent.parent / "edi"))
EDI_ENTRADA_DIR = EDI_DIR / "entrada"
EDI_PROCESSADOS_DIR = EDI_DIR / "processados"
STATIC_DIR = Path(__file__).parent / "static"

MAX_UPLOAD_FILES = int(os.getenv("MAX_UPLOAD_FILES", "200"))
MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_BYTES", str(5 * 1024 * 1024)))  # 5MB por CTe

_precos: Dict = {}
_prazos: Dict = {}


def _reload_all_tables() -> Dict[str, list]:
    """Carrega as tabelas que o motor de frete usa.

    A 'base de acordo' (banco, app.cadastro) é a FONTE ÚNICA do motor: quando ela
    tem transportadoras cadastradas, o motor serve exatamente o que está no banco —
    o Excel de demonstração não é mais misturado. O Excel só entra quando a base
    está vazia (1º boot): ele semeia a base e serve de fallback."""
    global _precos, _prazos
    transps = cadastro.list_transportadoras()
    if not transps:
        # Base vazia (1º boot): semeia a partir do Excel de demonstração (com CNPJs).
        try:
            criadas = cadastro.seed_all_from_excel(load_precos(PRECOS_FILE), load_prazos(PRAZOS_FILE))
            if criadas:
                logger.info(f"Base de acordo vazia — importadas {len(criadas)} transportadoras do Excel: {criadas}")
            transps = cadastro.list_transportadoras()
        except Exception:
            logger.exception("Falha ao semear a base de acordo a partir do Excel.")

    if transps:
        precos = cadastro.build_precos_overlay()
        prazos = cadastro.build_prazos_overlay()
    else:
        # Sem base e sem semente: usa o Excel diretamente (fallback de segurança).
        precos = load_precos(PRECOS_FILE)
        prazos = load_prazos(PRAZOS_FILE)

    _precos, _prazos = precos, prazos
    carriers = sorted(set(_precos) | set(_prazos))
    logger.info(f"Tabelas prontas. {len(carriers)} transportadoras: {carriers}")
    return {"precos": sorted(_precos), "prazos": sorted(_prazos)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Carregando tabelas de preços e prazos...")
    audit_history.init_db()
    tracking.init_db()
    cadastro.init_db()
    try:
        from app import taxas_condicionais
        taxas_condicionais.init_db()
    except Exception:
        logger.exception("Falha ao iniciar a base de taxas condicionais.")
    conferencia_mod.init_db()
    romaneio_mod.init_db()
    coletas_mod.init_db()
    vendas_mod.init_db()
    auth.init_db()
    access_log.init_db()
    _reload_all_tables()
    for d in (CTE_ENTRADA_DIR, CTE_OK_DIR, CTE_NOK_DIR, EDI_ENTRADA_DIR, EDI_PROCESSADOS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    olist_mod.iniciar_auto()   # NF-e do Olist -> fila do romaneio (se OLIST_TOKEN estiver no .env)
    vendas_mod.iniciar_auto()  # Pedidos de venda do Olist -> cache do painel de Análise de Vendas
    email_mod.iniciar_auto()   # carteiro de EDI: baixa CTe/OCOREN/DOCCOB do e-mail (se EMAIL_* no .env)
    ssw_sync_mod.iniciar_auto()  # ocorrências via WebAPI de rastreamento da SSW (se SSW_* no .env)
    jamef_webhook_mod.iniciar_auto()  # ocorrências da Jamef via recebedor de webhook isolado (se JAMEF_* no .env)
    yield
    olist_mod.parar_auto()
    vendas_mod.parar_auto()
    email_mod.parar_auto()
    ssw_sync_mod.parar_auto()
    jamef_webhook_mod.parar_auto()


app = FastAPI(
    title="Freight Gateway",
    description="Gateway de cotação de frete — padrão Intelipost, com suas transportadoras reais",
    version="2.0.0",
    lifespan=lifespan,
)


# Assets compartilhados dos painéis (volix.css, auth.js, manual PDF).
# no-cache: o navegador revalida a cada carga (304 barato se nada mudou), então
# uma edição de CSS/HTML aparece na hora — sem precisar de Ctrl+F5.
class _NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


app.mount("/static", _NoCacheStatic(directory=STATIC_DIR), name="static")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Detalhe do erro fica só no log do servidor; ao cliente vai uma mensagem
    # genérica — evita vazar caminhos de arquivo, SQL e stack, sobretudo quando o
    # painel é exposto numa rede (protegido pelo login por sessão).
    logger.exception(exc)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            message="Erro interno ao processar a solicitação. Consulte o log do servidor para detalhes."
        ).dict(),
    )


# Rotas acessíveis sem login: a própria tela de login, seus endpoints, os assets
# estáticos (CSS/JS/login precisam carregar) e o health check.
_AUTH_PUBLIC_EXACT = {"/health", "/login", "/api/login", "/api/logout", "/api/setup", "/api/auth/status", "/favicon.ico"}
_AUTH_PUBLIC_PREFIX = ("/static/",)

# Área de acesso de cada rota (páginas + prefixos de API). Rotas fora deste mapa
# são "gerais" (qualquer usuário logado): /home, /api/home, /api/feriados, /usuarios…
_AREA_POR_PREFIXO = (
    ("/audit", "financeiro"), ("/api/audit", "financeiro"), ("/api/carrier-mapping", "financeiro"),
    ("/percent-nf", "financeiro"), ("/api/percent-nf", "financeiro"),
    ("/conferencia", "financeiro"), ("/api/conferencia", "financeiro"),
    ("/romaneio", "expedicao"), ("/api/romaneio", "expedicao"),
    ("/coletas", "suprimentos"), ("/api/coletas", "suprimentos"),
    ("/tracking", "entregas"), ("/api/tracking", "entregas"),
    ("/relatorio-dia", "entregas"), ("/api/relatorio-diario", "entregas"),
    ("/scorecard", "entregas"), ("/api/scorecard", "entregas"),
    ("/ocorrencias", "entregas"), ("/api/ocorrencias", "entregas"),
    ("/vendas", "comercial"), ("/api/vendas", "comercial"),
    ("/cadastro", "config"), ("/api/cadastro", "config"),
)

# Endpoints de importação (subir/ler arquivos) — exigem a ação "pode_importar".
_IMPORT_PATHS = {
    "/api/audit", "/api/audit/scan-folder",
    "/api/tracking/ocoren", "/api/tracking/ocoren/scan-folder", "/api/tracking/ssw-sync",
    "/api/tracking/jamef-sync",
    "/api/romaneio/upload-xml", "/api/romaneio/import-pdf", "/api/romaneio/olist/sincronizar",
    "/api/romaneio/olist/ressincronizar-peso",
    "/api/vendas/sincronizar", "/api/email/ler",
    "/api/conferencia/preview", "/api/conferencia/preview-doccob", "/api/conferencia/faturas",
    "/api/coletas/importar-xml",
}


def _area_da_rota(path: str) -> Optional[str]:
    for prefixo, area in _AREA_POR_PREFIXO:
        if path == prefixo or path.startswith(prefixo + "/") or path.startswith(prefixo + "."):
            return area
    return None


def _client_ip(request: Request) -> str:
    """IP de origem do cliente. Considera X-Forwarded-For (modo rede/proxy) e cai
    no IP direto da conexão. Só para auditoria interna — não é usado para decisão."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def _auditar_acao_sensivel(request: Request, user: Optional[Dict], status_code: int) -> None:
    """Registra no log de acesso as ações sensíveis que completaram com sucesso
    (2xx/3xx): edição do cadastro, importação e exportação de relatório."""
    if status_code >= 400:
        return
    path, metodo = request.url.path, request.method
    ip = _client_ip(request)
    if path.endswith("/export.xlsx"):
        access_log.registrar_de_usuario("EXPORTACAO", user, detalhe=path, ip=ip)
    elif metodo == "POST" and path in _IMPORT_PATHS:
        access_log.registrar_de_usuario("IMPORTACAO", user, detalhe=path, ip=ip)
    elif path.startswith("/api/cadastro") and metodo in ("POST", "PUT", "DELETE"):
        access_log.registrar_de_usuario("CADASTRO_EDITADO", user, detalhe=f"{metodo} {path}", ip=ip)


@app.middleware("http")
async def session_auth_middleware(request: Request, call_next):
    """Exige login por usuário (sessão em cookie httponly) e aplica as permissões:
    acesso por área, baixar Excel, importar arquivos e editar cadastro. Rotas
    públicas passam livres; sem sessão → /login (página) ou 401 (API); com sessão
    mas sem permissão → /home (página) ou 403 (API). Admin é superusuário."""
    path = request.url.path
    user = auth.get_session_user(request.cookies.get(auth.COOKIE_NAME))
    request.state.user = user
    if path in _AUTH_PUBLIC_EXACT or any(path.startswith(p) for p in _AUTH_PUBLIC_PREFIX):
        return await call_next(request)
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"detail": "Não autenticado."})
        return RedirectResponse(url="/login")

    is_api = path.startswith("/api/")

    def _nega(msg: str):
        access_log.registrar_de_usuario("ACESSO_NEGADO", user,
                                        detalhe=f"{request.method} {path} — {msg}", ip=_client_ip(request))
        if is_api:
            return JSONResponse(status_code=403, content={"detail": msg})
        return RedirectResponse(url="/home")  # a Home é sempre visível

    # 1) Acesso por área
    area = _area_da_rota(path)
    if area and not auth.ve_area(user, area):
        return _nega("Você não tem acesso a esta área.")
    # 2) Baixar relatórios em Excel
    if path.endswith("/export.xlsx") and not auth.pode(user, "pode_exportar"):
        return _nega("Sem permissão para baixar relatórios em Excel.")
    # 3) Importar/subir arquivos
    if request.method == "POST" and path in _IMPORT_PATHS and not auth.pode(user, "pode_importar"):
        return _nega("Sem permissão para importar arquivos.")
    # 4) Editar a base de acordo (cadastro) — só escrita
    if (path.startswith("/api/cadastro") and request.method in ("POST", "PUT", "DELETE")
            and not auth.pode(user, "pode_editar_cadastro")):
        return _nega("Sem permissão para editar o cadastro.")

    response = await call_next(request)
    # As respostas de API refletem dados vivos (cache local do Olist, histórico, etc.).
    # Sem isto, o navegador pode servir um JSON ANTIGO após o servidor atualizar os
    # dados — ex.: um pedido de teste já removido continuar aparecendo no painel.
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    _auditar_acao_sensivel(request, user, response.status_code)
    # Editar a base de acordo revalida o cache de tabelas que a Auditoria/cotação
    # usam (_precos/_prazos são um overlay do banco montado no boot), para que a
    # mudança de preço/prazo valha na hora — sem depender do botão "Recarregar".
    if (path.startswith("/api/cadastro") and request.method in ("POST", "PUT", "DELETE")
            and response.status_code < 400):
        try:
            _reload_all_tables()
        except Exception:
            logger.warning("Falha ao revalidar as tabelas após edição do cadastro.", exc_info=True)
    return response


@app.get("/health")
async def health():
    return {
        "status": "OK",
        "transportadoras_precos": sorted(_precos.keys()),
        "transportadoras_prazos": sorted(_prazos.keys()),
    }


@app.get("/api/feriados", summary="Listar feriados considerados no cálculo de dias úteis (por ano)")
async def get_feriados(ano: Optional[int] = None):
    from datetime import date as _date
    ano = ano or _date.today().year
    dias = sorted(feriados.feriados_do_ano(ano))
    return {"ano": ano, "total": len(dias), "feriados": [d.isoformat() for d in dias]}


@app.post("/api/feriados/reload", summary="Recarregar a lista de feriados (tables/feriados.json) sem reiniciar")
async def reload_feriados():
    total = feriados.reload_feriados()
    return {"status": "OK", "feriados_ano_atual": total}


# ─── Auditor de CTe ──────────────────────────────────────────────────────────

# Sempre revalidar o HTML: evita que o navegador sirva uma versão antiga da
# página (cache) após atualizarmos os painéis. Sem isso, edições no HTML só
# aparecem depois de um Ctrl+F5 manual.
_NO_CACHE_HTML = {"Cache-Control": "no-cache, must-revalidate", "Pragma": "no-cache"}


# Faixa de aviso mostrada em TODAS as telas quando NÃO é produção (ex.: homologação).
# pointer-events:none para nunca atrapalhar cliques; fixa embaixo para não cobrir o topo.
_BANNER_HOMOLOG = (
    '<div style="position:fixed;bottom:0;left:0;right:0;z-index:2147483647;'
    'background:#c62828;color:#fff;text-align:center;'
    'font:600 12px/24px system-ui,Segoe UI,Arial,sans-serif;letter-spacing:.06em;'
    'pointer-events:none;box-shadow:0 -1px 6px rgba(0,0,0,.3);">'
    '⚠ HOMOLOGAÇÃO — ambiente de teste (NÃO é produção)</div>'
)


def _painel(nome_arquivo: str):
    ambiente = (os.getenv("LOGHUB_AMBIENTE", "producao") or "").strip().lower()
    if ambiente and ambiente not in ("producao", "produção", "prod"):
        # Ambiente de teste: injeta a faixa e marca a aba do navegador.
        try:
            html = (STATIC_DIR / nome_arquivo).read_text(encoding="utf-8")
            i = html.find("<body")
            j = html.find(">", i) if i != -1 else -1
            if j != -1:
                html = html[:j + 1] + _BANNER_HOMOLOG + html[j + 1:]
            html = html.replace("<title>", "<title>[HOMOLOG] ", 1)
            return HTMLResponse(html, headers=_NO_CACHE_HTML)
        except Exception:
            pass  # se algo falhar, cai no envio normal do arquivo
    return FileResponse(STATIC_DIR / nome_arquivo, media_type="text/html", headers=_NO_CACHE_HTML)


@app.get("/", include_in_schema=False)
async def root():
    # A raiz não serve nada por si; manda direto para o Home/Painel Executivo.
    return RedirectResponse(url="/home")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Ícone LogHub (aba do navegador e app instalado). Público — carrega antes do login.
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/home", response_class=HTMLResponse, summary="Home / Painel Executivo (visão consolidada)")
async def home_panel():
    return _painel("home.html")


@app.get("/audit", response_class=HTMLResponse, summary="Painel de auditoria de frete")
async def audit_panel():
    return _painel("audit.html")


@app.get("/tracking", response_class=HTMLResponse, summary="Torre de controle de entregas (OCOREN/OTD)")
async def tracking_panel():
    return _painel("tracking.html")


@app.get("/relatorio-dia", response_class=HTMLResponse, summary="Relatório diário de entregas por transportadora")
async def relatorio_dia_panel():
    return _painel("relatorio-dia.html")


@app.get("/scorecard", response_class=HTMLResponse, summary="Scorecard consolidado de transportadoras")
async def scorecard_panel():
    return _painel("scorecard.html")


@app.get("/percent-nf", response_class=HTMLResponse, summary="Custo de frete sobre faturamento (%NF)")
async def percent_nf_panel():
    return _painel("percent-nf.html")


@app.get("/ocorrencias", response_class=HTMLResponse, summary="Mapa de Ocorrências (não-conformidade)")
async def ocorrencias_panel():
    return _painel("ocorrencias.html")


@app.get("/cadastro", response_class=HTMLResponse, summary="Cadastro de transportadoras, tabelas de preço e prazo (base de acordo)")
async def cadastro_panel():
    return _painel("cadastro.html")


@app.get("/conferencia", response_class=HTMLResponse, summary="Conferência de fatura da transportadora")
async def conferencia_panel():
    return _painel("conferencia.html")


@app.get("/romaneio", response_class=HTMLResponse, summary="Romaneio de expedição (gerar por XML de NF-e / importar PDF)")
async def romaneio_panel():
    return _painel("romaneio.html")


@app.get("/coletas", response_class=HTMLResponse, summary="Coleta em fornecedores (agenda + custo de frete sobre a NF)")
async def coletas_panel():
    return _painel("coletas.html")


@app.get("/vendas", response_class=HTMLResponse, summary="Análise de Vendas (comercial): situações, faturamento por canal, produtos e preços — via Pedidos de Venda do Olist")
async def vendas_panel():
    return _painel("vendas.html")


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page():
    return _painel("login.html")


@app.get("/usuarios", response_class=HTMLResponse, summary="Gestão de usuários (somente administradores)")
async def usuarios_page():
    return _painel("usuarios.html")


@app.get("/log-acesso", response_class=HTMLResponse, summary="Log de acesso / auditoria (somente administradores)")
async def log_acesso_page():
    return _painel("log-acesso.html")


# ─── Autenticação e usuários ─────────────────────────────────────────────────

class LoginIn(BaseModel):
    login: str
    senha: str


class SetupIn(BaseModel):
    login: str
    nome: str = ""
    senha: str


class UsuarioIn(BaseModel):
    login: Optional[str] = None
    nome: Optional[str] = None
    senha: Optional[str] = None
    is_admin: Optional[bool] = None
    ativo: Optional[bool] = None
    pode_exportar: Optional[bool] = None
    pode_importar: Optional[bool] = None
    pode_editar_cadastro: Optional[bool] = None
    areas: Optional[List[str]] = None


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=auth.SESSION_HORAS * 3600, path="/",
    )


def _require_admin(request: Request) -> Dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Não autenticado.")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores.")
    return user


@app.get("/api/auth/status", summary="Situação da sessão atual (e se falta criar o 1º usuário)")
async def auth_status(request: Request):
    user = getattr(request.state, "user", None)
    return {"authenticated": user is not None, "precisa_setup": not auth.has_users(), "user": user}


@app.post("/api/login", summary="Autenticar (login + senha) e abrir sessão")
async def api_login(request: Request, body: LoginIn):
    ip = _client_ip(request)
    user = auth.verify_credentials(body.login, body.senha)
    if user is None:
        access_log.registrar("LOGIN_FALHA", login=body.login, detalhe="usuário ou senha inválidos", ip=ip)
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos.")
    token = auth.create_session(user["id"])
    access_log.registrar_de_usuario("LOGIN_OK", user, ip=ip)
    resp = JSONResponse({"status": "OK", "user": user})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/logout", summary="Encerrar a sessão atual")
async def api_logout(request: Request):
    user = getattr(request.state, "user", None)
    auth.delete_session(request.cookies.get(auth.COOKIE_NAME))
    if user:
        access_log.registrar_de_usuario("LOGOUT", user, ip=_client_ip(request))
    resp = JSONResponse({"status": "OK"})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.post("/api/setup", summary="Criar o primeiro administrador (só quando não há usuários)")
async def api_setup(request: Request, body: SetupIn):
    if auth.has_users():
        raise HTTPException(status_code=409, detail="Já existem usuários — o primeiro acesso já foi concluído.")
    try:
        user = auth.create_user(body.login, body.senha, body.nome, is_admin=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    token = auth.create_session(user["id"])
    access_log.registrar_de_usuario("SETUP_ADMIN", user, detalhe="primeiro administrador", ip=_client_ip(request))
    resp = JSONResponse({"status": "OK", "user": user})
    _set_session_cookie(resp, token)
    return resp


@app.get("/api/usuarios", summary="Listar usuários (admin)")
async def usuarios_list(request: Request):
    _require_admin(request)
    return auth.list_users()


@app.post("/api/usuarios", summary="Criar usuário (admin)")
async def usuarios_create(request: Request, body: UsuarioIn):
    admin = _require_admin(request)
    if not body.login or not body.senha:
        raise HTTPException(status_code=400, detail="Login e senha são obrigatórios.")
    try:
        novo = auth.create_user(
            body.login, body.senha, body.nome or "", bool(body.is_admin),
            pode_exportar=bool(body.pode_exportar), pode_importar=bool(body.pode_importar),
            pode_editar_cadastro=bool(body.pode_editar_cadastro), areas=body.areas,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail="Já existe um usuário com esse login.") from e
        raise
    access_log.registrar_de_usuario(
        "USUARIO_CRIADO", admin, ip=_client_ip(request),
        detalhe=f"criou '{novo['login']}'" + (" (admin)" if novo.get("is_admin") else ""))
    return novo


@app.put("/api/usuarios/{user_id}", summary="Atualizar usuário (admin)")
async def usuarios_update(request: Request, user_id: int, body: UsuarioIn):
    admin = _require_admin(request)
    alvo = auth.get_user(user_id)
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    # Impedir tirar o último administrador ativo (rebaixar ou desativar).
    rebaixando = body.is_admin is False and alvo["is_admin"]
    desativando = body.ativo is False and alvo["ativo"]
    if (rebaixando or desativando) and auth.count_admins(excluir_id=user_id) == 0:
        raise HTTPException(status_code=400, detail="Não é possível remover o último administrador ativo.")
    try:
        atualizado = auth.update_user(
            user_id, nome=body.nome, is_admin=body.is_admin, ativo=body.ativo, senha=body.senha,
            pode_exportar=body.pode_exportar, pode_importar=body.pode_importar,
            pode_editar_cadastro=body.pode_editar_cadastro, areas=body.areas,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    mudancas = []
    if body.senha:
        mudancas.append("senha")
    if body.is_admin is not None and body.is_admin != alvo["is_admin"]:
        mudancas.append("promoveu a admin" if body.is_admin else "removeu admin")
    if body.ativo is not None and body.ativo != alvo["ativo"]:
        mudancas.append("reativou" if body.ativo else "desativou")
    for campo in ("areas", "pode_exportar", "pode_importar", "pode_editar_cadastro"):
        if getattr(body, campo) is not None:
            mudancas.append(campo)
    resumo = ", ".join(mudancas) if mudancas else "dados básicos"
    access_log.registrar_de_usuario(
        "USUARIO_ALTERADO", admin, ip=_client_ip(request),
        detalhe=f"alterou '{alvo['login']}': {resumo}")
    return atualizado


@app.delete("/api/usuarios/{user_id}", summary="Excluir usuário (admin)")
async def usuarios_delete(request: Request, user_id: int):
    admin = _require_admin(request)
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Você não pode excluir o próprio usuário logado.")
    alvo = auth.get_user(user_id)
    if alvo is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    if alvo["is_admin"] and auth.count_admins(excluir_id=user_id) == 0:
        raise HTTPException(status_code=400, detail="Não é possível excluir o último administrador ativo.")
    auth.delete_user(user_id)
    access_log.registrar_de_usuario(
        "USUARIO_EXCLUIDO", admin, ip=_client_ip(request), detalhe=f"excluiu '{alvo['login']}'")
    return {"status": "OK"}


@app.get("/api/log-acesso", summary="Log de acesso / auditoria (admin) — paginado")
async def log_acesso_list(
    request: Request,
    evento: Optional[str] = None,
    usuario_id: Optional[int] = None,
    busca: Optional[str] = None,
    desde: Optional[str] = None,
    ate: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    _require_admin(request)
    agg = access_log.resumo(evento=evento, usuario_id=usuario_id, busca=busca, desde=desde, ate=ate)
    return {
        "eventos": access_log.EVENTOS,
        "usuarios": access_log.usuarios_no_log(),
        "total": agg["total"],
        "kpis": agg["por_evento"],
        "itens": access_log.listar(limit=limit, offset=offset, evento=evento, usuario_id=usuario_id,
                                   busca=busca, desde=desde, ate=ate),
    }


def _persist_results(results: List[AuditResult]) -> None:
    for r in results:
        audit_history.save_result(r)
        try:
            tracking.upsert_from_audit(r)
        except Exception:
            logger.exception(f"Falha registrando entrega na torre de controle: {r.arquivo}")
    # Casa automaticamente as devoluções (CT-e de retorno ↔ CT-e de ida) a cada
    # importação — assim o vínculo se forma sozinho quando o CT-e é importado,
    # sem depender de acionar "Casar devoluções" na tela.
    if results:
        try:
            tracking.reprocessar_devolucoes()
        except Exception:
            logger.exception("Falha ao casar devoluções após importação de CTe")


@app.post("/api/audit", response_model=List[AuditResult], summary="Auditar CTes enviados via upload")
async def audit_upload(
    files: List[UploadFile] = File(...),
    tol_abs: float = TOLERANCIA_ABS_PADRAO,
    tol_pct: float = TOLERANCIA_PCT_PADRAO,
):
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"Máximo de {MAX_UPLOAD_FILES} arquivos por envio.")

    payload = []
    for f in files:
        content = await f.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Arquivo '{f.filename}' excede o limite de {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
            )
        payload.append((f.filename, content))

    results = await audit_many(payload, _precos, _prazos, tol_abs, tol_pct)
    _persist_results(results)
    return results


def _unique_dest_path(dest_dir: Path, filename: str) -> Path:
    """Evita sobrescrever se já existir um arquivo com o mesmo nome em ok/nok."""
    dest = dest_dir / filename
    if not dest.exists():
        return dest
    stem, suffix = Path(filename).stem, Path(filename).suffix
    i = 2
    while True:
        candidate = dest_dir / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


@app.post(
    "/api/audit/scan-folder",
    response_model=List[AuditResult],
    summary="Auditar os XML da pasta de entrada e organizar em processados/ok e processados/nok",
)
async def audit_scan_folder(tol_abs: float = TOLERANCIA_ABS_PADRAO, tol_pct: float = TOLERANCIA_PCT_PADRAO):
    if not CTE_ENTRADA_DIR.exists():
        raise HTTPException(status_code=404, detail=f"Pasta de entrada {CTE_ENTRADA_DIR} não existe.")
    xml_files = sorted(glob.glob(str(CTE_ENTRADA_DIR / "*.xml")) + glob.glob(str(CTE_ENTRADA_DIR / "*.pdf")))
    if not xml_files:
        raise HTTPException(status_code=404, detail=f"Nenhum .xml/.pdf encontrado em {CTE_ENTRADA_DIR}.")
    if len(xml_files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"Pasta tem mais de {MAX_UPLOAD_FILES} arquivos — audite em lotes menores.")

    payload = []
    source_paths: Dict[str, Path] = {}
    for f in xml_files:
        path = Path(f)
        content = path.read_bytes()
        if len(content) > MAX_FILE_SIZE_BYTES:
            logger.warning(f"Ignorando {f}: excede {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.")
            continue
        payload.append((path.name, content))
        source_paths[path.name] = path

    results = await audit_many(payload, _precos, _prazos, tol_abs, tol_pct)
    _persist_results(results)

    for r in results:
        src = source_paths.get(r.arquivo)
        if src is None or not src.exists():
            continue
        dest_dir = CTE_OK_DIR if r.status in ("OK", "CANCELADO", "JA_LANCADO") else CTE_NOK_DIR
        dest = _unique_dest_path(dest_dir, src.name)
        try:
            shutil.move(str(src), str(dest))
        except OSError as e:
            logger.error(f"Falha ao mover {src} para {dest_dir}: {e}")

    return results


# ─── Histórico de auditoria ─────────────────────────────────────────────────

@app.get("/api/audit/history", summary="Listar histórico de auditorias com filtros (paginado)")
async def get_audit_history(
    mes: Optional[str] = None,
    transportadora: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    aprovacao: Optional[str] = None,
    offset: int = 0,
):
    return {
        "total": audit_history.count_history(mes, transportadora, status, search, aprovacao),
        "offset": max(0, offset),
        "limit": limit,
        "linhas": audit_history.query_history(mes, transportadora, status, search, limit, aprovacao, offset),
    }


@app.get("/api/audit/history/summary", summary="Resumo mensal do histórico (totais e divergências)")
async def get_audit_history_summary():
    return audit_history.summary_by_month()


@app.get("/api/audit/indicadores", summary="Indicadores estilo BI: KPIs, série mensal e ranking por transportadora")
async def get_audit_indicadores(mes: Optional[str] = None, transportadora: Optional[str] = None, status: Optional[str] = None):
    return audit_history.indicadores(mes, transportadora, status)


@app.get("/api/audit/history/meses", summary="Listar meses com auditorias registradas")
async def get_audit_history_meses():
    return audit_history.list_meses()


@app.get("/api/audit/history/export", summary="Exportar histórico filtrado em CSV")
async def export_audit_history(
    mes: Optional[str] = None,
    transportadora: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 5000,
):
    rows = audit_history.query_history(mes, transportadora, status, search, limit)
    csv_text = audit_history.export_csv(rows)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=historico_auditoria.csv"},
    )


@app.get("/api/audit/history/export.xlsx", summary="Exportar relatório de auditoria em Excel formatado")
async def export_audit_history_xlsx(
    mes: Optional[str] = None,
    transportadora: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 5000,
):
    rows = audit_history.query_history(mes, transportadora, status, search, limit)
    xlsx_bytes = audit_history.export_xlsx(
        rows, filtros={"mês": mes, "transportadora": transportadora, "status": status, "busca": search}
    )
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=relatorio_auditoria_frete.xlsx"},
    )


@app.get("/api/audit/history/{chave_cte}", summary="Buscar um registro do histórico pela chave do CTe")
async def get_audit_history_one(chave_cte: str):
    row = audit_history.get_by_chave(chave_cte)
    if row is None:
        raise HTTPException(status_code=404, detail="Registro não encontrado.")
    return row


class HistoryNoteIn(BaseModel):
    observacoes: str


@app.patch("/api/audit/history/{chave_cte}", summary="Atualizar observações de um registro do histórico")
async def patch_audit_history_notes(chave_cte: str, body: HistoryNoteIn):
    ok = audit_history.update_notes(chave_cte, body.observacoes)
    if not ok:
        raise HTTPException(status_code=404, detail="Registro não encontrado.")
    return {"status": "OK"}


class AprovacaoIn(BaseModel):
    decisao: str  # "APROVADO" ou "REPROVADO"
    justificativa: str = ""


@app.post(
    "/api/audit/history/{chave_cte}/aprovar",
    summary="Aprovar ou reprovar manualmente um CTe divergente (fica registrado quem decidiu e por quê)",
)
async def aprovar_audit_history(chave_cte: str, request: Request, body: AprovacaoIn):
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Não autenticado.")

    decisao = (body.decisao or "").strip().upper()
    if decisao not in ("APROVADO", "REPROVADO"):
        raise HTTPException(status_code=400, detail="decisao deve ser 'APROVADO' ou 'REPROVADO'.")
    if not body.justificativa.strip():
        raise HTTPException(status_code=400, detail="Justificativa é obrigatória para aprovar/reprovar manualmente.")

    alvo = audit_history.get_by_chave(chave_cte)
    if alvo is None:
        raise HTTPException(status_code=404, detail="Registro não encontrado.")
    if alvo["aprovacao_status"] == "AUTOMATICA":
        raise HTTPException(status_code=400, detail="CTe já foi aprovado automaticamente por bater com a tabela.")

    aprovador = (user.get("nome") or user.get("login") or "").strip()
    atualizado = audit_history.set_aprovacao(chave_cte, decisao, aprovador, body.justificativa.strip())
    if atualizado is None:
        raise HTTPException(status_code=404, detail="Registro não encontrado.")

    access_log.registrar_de_usuario(
        "AUDITORIA_APROVACAO", user, ip=_client_ip(request),
        detalhe=f"{decisao} CTe {chave_cte} — {body.justificativa.strip()[:200]}",
    )
    return atualizado


class VincularIdaIn(BaseModel):
    numero_nf: str


@app.post(
    "/api/audit/history/{chave_cte}/vincular-ida",
    summary="Vincular manualmente uma devolução/reentrega sem ida encontrada, digitando o nº da NF de origem",
)
async def vincular_ida_audit_history(chave_cte: str, request: Request, body: VincularIdaIn):
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Não autenticado.")
    if not body.numero_nf.strip():
        raise HTTPException(status_code=400, detail="Informe o número da NF de origem.")

    usuario = (user.get("nome") or user.get("login") or "").strip()
    try:
        atualizado = vincular_ida_manual(chave_cte, body.numero_nf.strip(), usuario=usuario)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    access_log.registrar_de_usuario(
        "AUDITORIA_VINCULO_MANUAL", user, ip=_client_ip(request),
        detalhe=f"CTe {chave_cte} vinculado à NF {body.numero_nf.strip()}",
    )
    return atualizado


@app.delete("/api/audit/history/{chave_cte}", summary="Remover um registro do histórico")
async def delete_audit_history_entry(chave_cte: str):
    ok = audit_history.delete_entry(chave_cte)
    if not ok:
        raise HTTPException(status_code=404, detail="Registro não encontrado.")
    return {"status": "OK"}


class CarrierMappingIn(BaseModel):
    cnpj: str
    carrier_name: Optional[str] = None  # None/"" => marca como SPOT conhecido


@app.get("/api/carrier-mapping", summary="Listar mapeamento CNPJ -> transportadora e transportadoras disponíveis")
async def get_carrier_mapping():
    return {
        "mapping": load_mapping(),
        "spot_sentinel": SPOT_SENTINEL,
        "transportadoras_disponiveis": sorted(set(_precos) | set(_prazos)),
    }


@app.post("/api/carrier-mapping", summary="Cadastrar/atualizar mapeamento CNPJ -> transportadora")
async def post_carrier_mapping(body: CarrierMappingIn):
    mapping = set_mapping(body.cnpj, body.carrier_name)
    return {"status": "OK", "mapping": mapping}


# ─── Torre de Controle de Entregas (OCOREN / tracking) ──────────────────────

_OCOREN_EXTENSOES = {".txt", ".oco", ".edi", ".ocr", ".pro"}


async def _importar_ocoren_payload(payload: List[tuple]) -> List[Dict]:
    relatorios = []
    houve_import = False
    for filename, content in payload:
        try:
            parsed = parse_ocoren_bytes(content, filename=filename)
            relatorios.append(tracking.import_ocoren(parsed))
            houve_import = True
        except OcorenParseError as e:
            relatorios.append({"arquivo": filename, "erro": str(e)})
        except Exception as e:
            logger.exception(f"Erro inesperado importando OCOREN {filename}")
            relatorios.append({"arquivo": filename, "erro": f"Erro inesperado: {e}"})

    # Ocorrência de entrega pode ter completado um romaneio: encerra na hora os
    # romaneios cujas NFs foram todas entregues (não espera abrir a tela de Romaneio).
    if houve_import:
        try:
            n = romaneio_mod.reconciliar_encerrados()
            if n:
                logger.info(f"OCOREN: {n} romaneio(s) encerrado(s) por entrega completa.")
                for r in relatorios:
                    if "erro" not in r:
                        r["romaneios_encerrados"] = n  # marca no 1º relatório válido
                        break
        except Exception:
            logger.exception("Falha ao reconciliar encerramento de romaneios após OCOREN")
    return relatorios


@app.post("/api/tracking/ocoren", summary="Importar arquivos EDI OCOREN (ocorrências de entrega) via upload")
async def tracking_import_ocoren(files: List[UploadFile] = File(...)):
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"Máximo de {MAX_UPLOAD_FILES} arquivos por envio.")
    payload = []
    for f in files:
        content = await f.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Arquivo '{f.filename}' excede o limite de {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
            )
        payload.append((f.filename, content))
    return await _importar_ocoren_payload(payload)


@app.post(
    "/api/tracking/ocoren/scan-folder",
    summary="Importar os arquivos OCOREN da pasta edi/entrada e movê-los para edi/processados",
)
async def tracking_scan_folder():
    if not EDI_ENTRADA_DIR.exists():
        raise HTTPException(status_code=404, detail=f"Pasta de entrada {EDI_ENTRADA_DIR} não existe.")
    arquivos = sorted(
        p for p in EDI_ENTRADA_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _OCOREN_EXTENSOES
    )
    if not arquivos:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum arquivo OCOREN ({', '.join(sorted(_OCOREN_EXTENSOES))}) encontrado em {EDI_ENTRADA_DIR}.",
        )

    payload = [(p.name, p.read_bytes()) for p in arquivos if p.stat().st_size <= MAX_FILE_SIZE_BYTES]
    relatorios = await _importar_ocoren_payload(payload)

    # Só move para processados quem realmente importou algo (novas ou duplicatas de
    # importação anterior). Arquivo com erro ou 100% rejeitado permanece na entrada
    # para diagnóstico/correção do parser.
    ok_por_arquivo = {
        r["arquivo"]: "erro" not in r and (r["ocorrencias_novas"] + r["ocorrencias_duplicadas"]) > 0
        for r in relatorios
    }
    for p in arquivos:
        if not ok_por_arquivo.get(p.name):
            continue
        dest = _unique_dest_path(EDI_PROCESSADOS_DIR, p.name)
        try:
            shutil.move(str(p), str(dest))
        except OSError as e:
            logger.error(f"Falha ao mover {p} para {EDI_PROCESSADOS_DIR}: {e}")
    return relatorios


@app.get("/api/tracking", summary="Listar entregas da torre de controle com status calculado")
async def tracking_list(
    status: Optional[str] = None,
    transportadora: Optional[str] = None,
    mes: Optional[str] = None,
    uf: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
):
    return tracking.query_entregas(status, transportadora, mes, uf, search, limit)


@app.get("/api/tracking/kpis", summary="KPIs de entrega: OTD geral e por transportadora")
async def tracking_kpis(mes: Optional[str] = None, transportadora: Optional[str] = None,
                        uf: Optional[str] = None):
    return tracking.kpis(mes, transportadora, uf)


@app.get("/api/tracking/alertas", summary="Entregas em risco (prazo vencendo sem sinal de saída) + cobertura de rastreio por transportadora")
async def tracking_alertas(dias: int = 2, mes: Optional[str] = None,
                           transportadora: Optional[str] = None, uf: Optional[str] = None):
    return {
        "dias": dias,
        "em_risco": tracking.entregas_em_risco(dias, transportadora, mes, uf),
        "cobertura": tracking.cobertura_rastreio(mes, transportadora),
    }


@app.get("/api/tracking/meses", summary="Meses com entregas registradas")
async def tracking_meses():
    return tracking.list_meses()


@app.get("/api/tracking/ufs", summary="UFs (estados) com entregas registradas")
async def tracking_ufs():
    return tracking.list_ufs()


def _nome_relatorio(transportadora: Optional[str], mes: Optional[str], ext: str) -> str:
    partes = ["relatorio_entregas"]
    tl = [t.strip() for t in (transportadora or "").split(",") if t.strip()]
    if len(tl) == 1:
        partes.append("".join(c for c in tl[0] if c.isalnum() or c in "-_"))
    elif len(tl) > 1:
        partes.append(f"{len(tl)}transp")
    ml = [m.strip() for m in (mes or "").split(",") if m.strip()]
    if len(ml) == 1:
        partes.append(ml[0])
    elif len(ml) > 1:
        partes.append(f"{len(ml)}meses")
    return "_".join(partes) + ext


@app.get("/api/tracking/export.xlsx", summary="Exportar relatório de entregas em Excel (Entregas + OTD + Ocorrências)")
async def tracking_export_xlsx(
    status: Optional[str] = None,
    transportadora: Optional[str] = None,
    mes: Optional[str] = None,
    uf: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 10000,
):
    rows = tracking.query_entregas(status, transportadora, mes, uf, search, limit)
    kpis_data = tracking.kpis(entregas=rows)
    status_label = ", ".join(
        tracking.STATUS_LABEL.get(s.strip(), s.strip())
        for s in (status or "").split(",") if s.strip()
    )
    xlsx_bytes = tracking.export_xlsx(
        rows, kpis_data,
        filtros={"transportadora": transportadora, "mês": mes, "UF": uf,
                 "status": status_label, "busca": search},
    )
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={_nome_relatorio(transportadora, mes, '.xlsx')}"},
    )


@app.get("/api/tracking/export", summary="Exportar entregas filtradas em CSV")
async def tracking_export_csv(
    status: Optional[str] = None,
    transportadora: Optional[str] = None,
    mes: Optional[str] = None,
    uf: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 10000,
):
    rows = tracking.query_entregas(status, transportadora, mes, uf, search, limit)
    csv_text = tracking.export_csv(rows)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={_nome_relatorio(transportadora, mes, '.csv')}"},
    )


@app.get("/api/tracking/ocorrencias-sem-vinculo", summary="Ocorrências importadas ainda sem CTe correspondente")
async def tracking_sem_vinculo(limit: int = 200):
    return tracking.ocorrencias_sem_vinculo(limit)


@app.post("/api/tracking/codigos/reload", summary="Recarregar a tabela de códigos de ocorrência do disco e re-aplicar às ocorrências")
async def tracking_reload_codigos():
    codigos = tracking.load_codigos(force=True)
    tracking.load_culpa(force=True)  # recarrega a classificação de culpa das tentativas
    # Re-aplica a tabela às ocorrências já gravadas (a classificação é congelada na
    # importação): assim, editar/recarregar os códigos atualiza a base de uma vez.
    reclass = tracking.reclassificar_ocorrencias()
    return {"status": "OK", "total_codigos": len(codigos), **reclass}


@app.post("/api/tracking/unificar-transportadoras", summary="Unificar na Torre as filiais/CNPJs de cada transportadora (grupo por raiz do CNPJ)")
async def tracking_unificar_transportadoras():
    return {"status": "OK", **tracking.recanonizar_transportadoras()}


@app.post("/api/tracking/unificar-ctes", summary="Unificar CT-es secundários (reentrega/complementar/redespacho) no CT-e de origem")
async def tracking_unificar_ctes():
    return {"status": "OK", **tracking.reprocessar_unificacao()}


@app.post("/api/tracking/casar-devolucoes", summary="Casar CT-es de devolução com o CT-e de ida (por NF e por cliente) e marcá-los")
async def tracking_casar_devolucoes():
    return {"status": "OK", **tracking.reprocessar_devolucoes()}


@app.post(
    "/api/tracking/reprocessar-prazos",
    summary="Recalcular a data prevista de todas as entregas (base: liberação do romaneio; regra atual de feriados)",
)
async def tracking_reprocessar_prazos():
    return {"status": "OK", **tracking.reprocessar_datas_previstas()}


@app.post(
    "/api/tracking/religar-ocorrencias",
    summary="Religar ocorrências órfãs (que apontavam para provisório de romaneio já apagado) ao CTe real",
)
async def tracking_religar_ocorrencias():
    return {"status": "OK", **tracking.religar_ocorrencias_orfas()}


@app.get("/api/tracking/{chave_cte}/timeline", summary="Linha do tempo de uma entrega (NFs + ocorrências)")
async def tracking_timeline(chave_cte: str):
    data = tracking.timeline(chave_cte)
    if data is None:
        raise HTTPException(status_code=404, detail="Entrega não encontrada.")
    return data


class EntregaManualIn(BaseModel):
    data_entrega: Optional[str] = None  # ISO "YYYY-MM-DD"; None remove a baixa manual


def _normalizar_data_entrega(valor: str) -> str:
    """Aceita AAAA-MM-DD, DD/MM/AAAA (ou DD-MM-AAAA) e datetimes ISO com hora,
    devolvendo sempre AAAA-MM-DD. Lança ValueError se não reconhecer."""
    from datetime import date as _date, datetime as _dt
    v = (valor or "").strip()
    if not v:
        raise ValueError("vazio")
    # datetime ISO completo (ex.: 2026-09-10T14:30:00 ou "2026-09-10 14:30")
    base = v.replace("T", " ").split(" ")[0]
    # AAAA-MM-DD
    try:
        return _date.fromisoformat(base).isoformat()
    except ValueError:
        pass
    # DD/MM/AAAA ou DD-MM-AAAA
    partes = base.replace("/", "-").split("-")
    if len(partes) == 3 and len(partes[0]) <= 2:
        d, m, a = partes
        return _date(int(a), int(m), int(d)).isoformat()
    raise ValueError(f"formato de data não reconhecido: {valor!r}")


@app.patch("/api/tracking/{chave_cte}", summary="Registrar (ou remover) baixa manual de entrega")
async def tracking_entrega_manual(chave_cte: str, body: EntregaManualIn):
    if body.data_entrega:
        try:
            body.data_entrega = _normalizar_data_entrega(body.data_entrega)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="Data inválida. Use o formato DD/MM/AAAA ou AAAA-MM-DD.",
            ) from None
    ok = tracking.marcar_entrega_manual(chave_cte, body.data_entrega)
    if not ok:
        raise HTTPException(status_code=404, detail="Entrega não encontrada.")
    return {"status": "OK"}


class DevolucaoIn(BaseModel):
    chave_devolucao: str          # CT-e de retorno
    chave_original: Optional[str] = None  # CT-e original (obrigatório ao vincular)


@app.post("/api/tracking/devolucao/vincular", summary="Vincular um CT-e de devolução ao CT-e original da mesma mercadoria")
async def tracking_vincular_devolucao(body: DevolucaoIn):
    try:
        res = tracking.vincular_devolucao(body.chave_devolucao, body.chave_original or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"status": "OK", **res}


class DevolucaoNfIn(BaseModel):
    chave_devolucao: str                  # CT-e de retorno
    numero_nf: str                        # nº da NF de ORIGEM
    cnpj_emissor: Optional[str] = None    # opcional — restringe o emissor da NF


@app.post("/api/tracking/devolucao/vincular-nf", summary="Vincular um CT-e de devolução informando a NF de ORIGEM (acha o CT-e original por ela)")
async def tracking_vincular_devolucao_nf(body: DevolucaoNfIn):
    try:
        res = tracking.vincular_devolucao_por_nf(body.chave_devolucao, body.numero_nf, body.cnpj_emissor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"status": "OK", **res}


@app.post("/api/tracking/devolucao/desvincular", summary="Desfazer o vínculo de devolução de um CT-e")
async def tracking_desvincular_devolucao(body: DevolucaoIn):
    return {"status": "OK", **tracking.desvincular_devolucao(body.chave_devolucao)}


@app.get("/api/relatorio-diario", summary="Relatório diário por transportadora (vencidas/vencem hoje/próximas + desempenho)")
async def relatorio_diario(dias: int = 2, formato: str = "json", transportadora: Optional[str] = None):
    rel = relatorio_mod.gerar(dias_proximo=max(1, min(int(dias or 2), 10)))
    if formato == "html":
        return HTMLResponse(relatorio_mod.render_html(rel))
    if formato == "texto":
        return PlainTextResponse(relatorio_mod.render_texto(rel, transportadora=transportadora),
                                 media_type="text/plain; charset=utf-8")
    return rel


@app.get("/api/relatorio-diario/analise", summary="Análise detalhada por transportadora (desempenho por estado + pontos de atenção)")
async def relatorio_analise(formato: str = "json"):
    an = relatorio_mod.analise_detalhada()
    if formato == "html":
        return HTMLResponse(relatorio_mod.render_analise_html(an))
    return an


# ─── Scorecard consolidado de transportadoras ───────────────────────────────

@app.get("/api/scorecard", summary="Desempenho de entrega: resumo, por transportadora, mensal e por estado")
async def get_scorecard(mes: Optional[str] = None, uf: Optional[str] = None, transp: Optional[str] = None):
    return scorecard_mod.scorecard(mes, uf, transp)


@app.get("/api/scorecard/meses", summary="Meses com entregas registradas")
async def get_scorecard_meses():
    return scorecard_mod.list_meses()


@app.get("/api/scorecard/export.xlsx", summary="Exportar scorecard de desempenho em Excel")
async def export_scorecard_xlsx(mes: Optional[str] = None, uf: Optional[str] = None, transp: Optional[str] = None):
    data = scorecard_mod.scorecard(mes, uf, transp)
    xlsx_bytes = scorecard_mod.export_xlsx(data)
    uf_slug = "-".join(data.get("uf") or [])
    transp_slug = "".join(c for c in (data.get("transp") or "") if c.isalnum())
    sufixo = "_".join(p for p in [mes, transp_slug, uf_slug] if p)
    nome = f"scorecard_desempenho{('_' + sufixo) if sufixo else ''}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={nome}"},
    )


# ─── Custo de Frete sobre Faturamento (%NF) ─────────────────────────────────

@app.get("/api/percent-nf", summary="%NF: resumo, por transportadora, mensal e por estado")
async def get_percent_nf(mes: Optional[str] = None, uf: Optional[str] = None, escopo: str = "distribuicao", transp: Optional[str] = None, op: Optional[str] = None):
    return percent_nf_mod.percent_nf(mes, uf, escopo, transp, op)


@app.get("/api/percent-nf/meses", summary="Meses com CTes auditados (base do %NF)")
async def get_percent_nf_meses():
    return percent_nf_mod.list_meses()


class NfMetaPair(BaseModel):
    meta: float
    atencao: float


class NfMetasIn(BaseModel):
    default: NfMetaPair
    por_uf: Dict[str, NfMetaPair]


@app.get("/api/percent-nf/metas", summary="Metas de %NF por estado (UF) + padrão")
async def get_percent_nf_metas():
    return percent_nf_mod.load_metas()


@app.put("/api/percent-nf/metas", summary="Salvar metas de %NF por estado (UF)")
async def put_percent_nf_metas(body: NfMetasIn):
    data = percent_nf_mod.save_metas(
        body.default.dict(),
        {uf: p.dict() for uf, p in body.por_uf.items()},
    )
    return {"status": "OK", **data}


@app.get("/api/percent-nf/export.xlsx", summary="Exportar %NF em Excel")
async def export_percent_nf_xlsx(mes: Optional[str] = None, uf: Optional[str] = None, escopo: str = "distribuicao", transp: Optional[str] = None, op: Optional[str] = None):
    data = percent_nf_mod.percent_nf(mes, uf, escopo, transp, op)
    xlsx_bytes = percent_nf_mod.export_xlsx(data)
    sufixo = "".join(p for p in [mes, (uf or "").upper() or None, (escopo if escopo != "distribuicao" else None)] if p)
    nome = f"percent_nf_faturamento{('_' + sufixo) if sufixo else ''}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={nome}"},
    )


@app.get("/api/percent-nf/ctes", summary="Listar CTes para marcação de reentrega/devolução (busca por nº/chave)")
async def percent_nf_ctes(search: Optional[str] = None, mes: Optional[str] = None, limit: int = 60):
    rows = audit_history.query_history(mes=mes, search=search, limit=limit)
    return [
        {
            "chave_cte": r.get("chave_cte"),
            "numero_cte": r.get("numero_cte"),
            "data_emissao": (r.get("data_emissao") or "")[:10],
            "transportadora": r.get("transportadora_tabela") or r.get("transportadora_nome_cte") or "",
            "origem": r.get("origem"),
            "destino": r.get("destino"),
            "valor_cobrado": r.get("valor_cobrado"),
            "tipo_auto": (r.get("tipo_operacao") or None),
            "tipo_manual": (r.get("tipo_operacao_manual") or None),
            "tipo_efetivo": percent_nf_mod.tipo_efetivo(r),
        }
        for r in rows
    ]


class TipoOperacaoIn(BaseModel):
    chave_cte: str
    tipo: Optional[str] = None  # REENTREGA | DEVOLUCAO | NORMAL | ""/None (limpa o override)


@app.post("/api/percent-nf/marcar-operacao", summary="Marcar manualmente o tipo de operação de um CTe (reentrega/devolução/normal)")
async def percent_nf_marcar_operacao(body: TipoOperacaoIn):
    try:
        ok = audit_history.set_tipo_operacao_manual(body.chave_cte, body.tipo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="CTe não encontrado no histórico.")
    return {"status": "OK"}


# ─── Mapa de Ocorrências ─────────────────────────────────────────────────────

@app.get("/api/ocorrencias", summary="Mapa de ocorrências: resumo, por transportadora, estado, motivos")
async def get_ocorrencias(mes: Optional[str] = None, uf: Optional[str] = None):
    return ocorrencias_mod.mapa(mes, uf)


@app.get("/api/ocorrencias/reincidencia", summary="Cidades × transportadora com ocorrências recorrentes (praças-problema)")
async def get_reincidencia(mes: Optional[str] = None, uf: Optional[str] = None, min: int = 2):
    return {"cidades": ocorrencias_mod.reincidencia_por_cidade(mes, uf, min)}


@app.get("/api/ocorrencias/meses", summary="Meses com entregas auditadas (base do mapa)")
async def get_ocorrencias_meses():
    return ocorrencias_mod.list_meses()


class OcMetaPair(BaseModel):
    meta: float
    atencao: float


class OcMetasIn(BaseModel):
    taxa_ocorrencia: OcMetaPair
    sinistro: OcMetaPair
    devolucao: OcMetaPair
    tentativa_frustrada: OcMetaPair


@app.get("/api/ocorrencias/metas", summary="Metas de ocorrência por categoria")
async def get_ocorrencias_metas():
    return ocorrencias_mod.load_metas()


@app.put("/api/ocorrencias/metas", summary="Salvar metas de ocorrência por categoria")
async def put_ocorrencias_metas(body: OcMetasIn):
    data = ocorrencias_mod.save_metas(body.dict())
    return {"status": "OK", **data}


@app.get("/api/ocorrencias/export.xlsx", summary="Exportar Mapa de Ocorrências em Excel")
async def export_ocorrencias_xlsx(mes: Optional[str] = None, uf: Optional[str] = None):
    data = ocorrencias_mod.mapa(mes, uf)
    xlsx_bytes = ocorrencias_mod.export_xlsx(data)
    sufixo = "".join(p for p in [mes, (uf or "").upper() or None] if p)
    nome = f"mapa_ocorrencias{('_' + sufixo) if sufixo else ''}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={nome}"},
    )


# ─── Cadastro / Base de Acordo ───────────────────────────────────────────────

class TransportadoraIn(BaseModel):
    nome: Optional[str] = None
    apelido: Optional[str] = None
    origem: Optional[str] = None
    ativo: Optional[bool] = None
    cor: Optional[str] = None
    cnpjs: Optional[List[str]] = None
    meta_otd: Optional[float] = None
    atencao_otd: Optional[float] = None
    tol_div_abs: Optional[float] = None
    tol_div_pct: Optional[float] = None
    gris_pct: Optional[float] = None
    gris_min: Optional[float] = None
    pedagio_ref: Optional[float] = None
    tas: Optional[float] = None
    despacho: Optional[float] = None
    tec_pct: Optional[float] = None
    cubagem_fator: Optional[float] = None
    pos_pct: Optional[float] = None
    pos_min: Optional[float] = None
    trt_pct: Optional[float] = None
    trt_min: Optional[float] = None
    icms_embutido: Optional[float] = None


class PrecoRow(BaseModel):
    uf: str = ""
    regiao: str = ""
    descricao: str = ""
    faixas: Dict[str, float] = {}
    advalorem_pct: float = 0.0
    frete_val_pct: float = 0.0
    sec_cat: float = 0.0
    pedagio_col: float = 0.0
    despacho: Optional[float] = None


class PrazoRow(BaseModel):
    id: Optional[int] = None
    uf: str = ""
    cidade: str = ""
    regiao: str = ""
    cep_inicial: Optional[str] = None
    cep_final: Optional[str] = None
    prazo_dias: int = 0


class VigenciaIn(BaseModel):
    vigencia_inicio: str          # AAAA-MM-DD — data a partir da qual a nova tabela vale
    descricao: str = ""
    tda: str = ""


@app.get("/api/cadastro/transportadoras", summary="Listar transportadoras cadastradas (base de acordo)")
async def cadastro_list():
    return cadastro.list_transportadoras()


@app.post("/api/cadastro/transportadoras", summary="Cadastrar transportadora")
async def cadastro_create(body: TransportadoraIn):
    try:
        return cadastro.create_transportadora(body.dict(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail="Já existe transportadora com esse nome ou CNPJ.") from e
        raise


@app.put("/api/cadastro/transportadoras/{transp_id}", summary="Atualizar transportadora")
async def cadastro_update(transp_id: int, body: TransportadoraIn):
    try:
        result = cadastro.update_transportadora(transp_id, body.dict(exclude_unset=True))
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail="Já existe transportadora com esse nome ou CNPJ.") from e
        raise
    if result is None:
        raise HTTPException(status_code=404, detail="Transportadora não encontrada.")
    return result


@app.delete("/api/cadastro/transportadoras/{transp_id}", summary="Excluir transportadora")
async def cadastro_delete(transp_id: int):
    if not cadastro.delete_transportadora(transp_id):
        raise HTTPException(status_code=404, detail="Transportadora não encontrada.")
    return {"status": "OK"}


@app.get("/api/cadastro/transportadoras/{transp_id}/precos", summary="Listar linhas de preço da transportadora")
async def cadastro_list_precos(transp_id: int):
    if cadastro.get_transportadora(transp_id) is None:
        raise HTTPException(status_code=404, detail="Transportadora não encontrada.")
    return cadastro.list_precos(transp_id)


@app.put("/api/cadastro/transportadoras/{transp_id}/precos", summary="Salvar (substituir) linhas de preço")
async def cadastro_save_precos(transp_id: int, linhas: List[PrecoRow]):
    try:
        return cadastro.save_precos(transp_id, [l.dict() for l in linhas])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.delete("/api/cadastro/transportadoras/{transp_id}/precos/{rid}", summary="Remover uma linha de preço")
async def cadastro_delete_preco(transp_id: int, rid: int):
    if not cadastro.delete_linha("tabela_preco", transp_id, rid):
        raise HTTPException(status_code=404, detail="Linha não encontrada.")
    return {"status": "OK"}


@app.get("/api/cadastro/transportadoras/{transp_id}/vigencias", summary="Vigência atual + histórico de tabelas arquivadas")
async def cadastro_list_vigencias(transp_id: int):
    if cadastro.get_transportadora(transp_id) is None:
        raise HTTPException(status_code=404, detail="Transportadora não encontrada.")
    return cadastro.list_vigencias(transp_id)


@app.post("/api/cadastro/transportadoras/{transp_id}/vigencia", summary="Registrar reajuste: arquiva a tabela atual e abre nova vigência")
async def cadastro_registrar_vigencia(transp_id: int, body: VigenciaIn):
    if cadastro.get_transportadora(transp_id) is None:
        raise HTTPException(status_code=404, detail="Transportadora não encontrada.")
    try:
        res = cadastro.arquivar_e_reajustar(transp_id, body.vigencia_inicio, body.descricao)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"status": "OK", **res}


@app.get("/api/cadastro/transportadoras/{transp_id}/simular-reajuste", summary="Simular o impacto de um reajuste % sobre os CTes históricos")
async def cadastro_simular_reajuste(transp_id: int, pct: float, mes: Optional[str] = None):
    t = cadastro.get_transportadora(transp_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Transportadora não encontrada.")
    carrier_name = t["nome"]
    precos_data = _precos.get(carrier_name)
    prazos_df = _prazos.get(carrier_name)
    if precos_data is None or prazos_df is None:
        raise HTTPException(status_code=400, detail="Sem tabela de preço/prazo carregada para esta transportadora.")
    historico = audit_history.query_history(mes=mes, transportadora=carrier_name, limit=100000)
    return simulador_mod.simular(carrier_name, precos_data, prazos_df, historico, pct)


@app.get("/api/cadastro/transportadoras/{transp_id}/prazos", summary="Listar linhas de prazo (paginado, com busca)")
async def cadastro_list_prazos(transp_id: int, search: Optional[str] = None, limit: int = 500, offset: int = 0):
    if cadastro.get_transportadora(transp_id) is None:
        raise HTTPException(status_code=404, detail="Transportadora não encontrada.")
    return cadastro.list_prazos(transp_id, search, limit, offset)


@app.put("/api/cadastro/transportadoras/{transp_id}/prazos", summary="Salvar (substituir) linhas de prazo")
async def cadastro_save_prazos(transp_id: int, linhas: List[PrazoRow]):
    try:
        return cadastro.save_prazos(transp_id, [l.dict() for l in linhas])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.patch("/api/cadastro/transportadoras/{transp_id}/prazos", summary="Upsert (atualiza/insere) linhas de prazo sem apagar as demais")
async def cadastro_upsert_prazos(transp_id: int, linhas: List[PrazoRow]):
    try:
        return cadastro.upsert_prazos(transp_id, [l.dict() for l in linhas])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.delete("/api/cadastro/transportadoras/{transp_id}/prazos/{rid}", summary="Remover uma linha de prazo")
async def cadastro_delete_prazo(transp_id: int, rid: int):
    if not cadastro.delete_linha("tabela_prazo", transp_id, rid):
        raise HTTPException(status_code=404, detail="Linha não encontrada.")
    return {"status": "OK"}


@app.post("/api/cadastro/transportadoras/{transp_id}/seed-excel", summary="Importar linhas da planilha Excel de mesmo nome para o banco")
async def cadastro_seed_excel(transp_id: int):
    try:
        resultado = cadastro.seed_from_excel(transp_id, load_precos(PRECOS_FILE), load_prazos(PRAZOS_FILE))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if resultado["precos"] == 0 and resultado["prazos"] == 0:
        raise HTTPException(status_code=404, detail="Nenhuma aba com o nome dessa transportadora foi encontrada no Excel.")
    return {"status": "OK", **resultado}


@app.post("/api/cadastro/importar-todas", summary="Importar para a base todas as transportadoras das planilhas Excel ainda não cadastradas")
async def cadastro_importar_todas():
    criadas = cadastro.seed_all_from_excel(load_precos(PRECOS_FILE), load_prazos(PRAZOS_FILE))
    _reload_all_tables()
    return {"status": "OK", "importadas": criadas}


@app.post("/api/cadastro/reload", summary="Aplicar o cadastro ao motor de frete (recarrega Excel + banco) e propagar prazos à Torre")
async def cadastro_reload():
    info = _reload_all_tables()
    # Propaga mudanças da tabela de prazo às entregas já na Torre (o prazo é gravado
    # como snapshot na auditoria e, sozinho, não acompanha a edição da tabela).
    prazos_torre = tracking.reprocessar_prazos_cadastro(_prazos)
    # Unifica na Torre as transportadoras cujo CNPJ (grupo) mudou — junta filiais/
    # logística reversa sob o mesmo nome.
    grupos_torre = tracking.recanonizar_transportadoras()
    return {"status": "OK", **info, "torre_prazos_reprocessados": prazos_torre,
            "torre_transportadoras_unificadas": grupos_torre}


@app.get("/api/cadastro/comparativo", summary="Comparar as tabelas das transportadoras para um destino (faixa de peso e generalidades)")
async def cadastro_comparativo(cep: str, peso: Optional[float] = None, valor: Optional[float] = None):
    cep8 = "".join(c for c in cep if c.isdigit()).zfill(8)
    if len(cep8) != 8:
        raise HTTPException(status_code=400, detail="Informe um CEP de destino com 8 dígitos.")
    city_uf = await resolve_cep(cep8)
    return comparativo_mod.comparar(cep8, _precos, _prazos, peso, valor, city_uf)


def _nome_arquivo_seguro(nome: str) -> str:
    return "".join(c for c in nome if c.isalnum() or c in "-_") or "transportadora"


@app.get("/api/cadastro/modelo.xlsx", summary="Baixar o modelo padrão de planilha de tabela de frete (em branco)")
async def cadastro_modelo():
    xlsx = cadastro_planilha.gerar_modelo()
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=modelo_tabela_frete.xlsx"},
    )


@app.get("/api/cadastro/transportadoras/{transp_id}/export.xlsx", summary="Exportar a tabela de uma transportadora no formato do modelo")
async def cadastro_export(transp_id: int):
    transp = cadastro.get_transportadora(transp_id)
    if transp is None:
        raise HTTPException(status_code=404, detail="Transportadora não encontrada.")
    precos = cadastro.list_precos(transp_id)
    prazos = cadastro.list_prazos(transp_id, limit=1_000_000)["linhas"]
    xlsx = cadastro_planilha.gerar_modelo(transp, precos, prazos)
    nome = _nome_arquivo_seguro(transp["nome"])
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=tabela_{nome}.xlsx"},
    )


@app.post("/api/cadastro/importar", summary="Importar uma planilha (modelo) e criar/atualizar a transportadora")
async def cadastro_importar_planilha(file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail=f"Arquivo excede {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.")
    try:
        dados = cadastro_planilha.ler_planilha(content)
    except Exception as e:
        logger.exception("Falha lendo planilha de cadastro")
        raise HTTPException(status_code=400, detail=f"Não foi possível ler a planilha: {e}") from e
    try:
        resultado = cadastro.importar_planilha(dados)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _reload_all_tables()
    return {"status": "OK", **resultado}


# ─── Conferência de Fatura da Transportadora ────────────────────────────────

class FaturaItemIn(BaseModel):
    chave: Optional[str] = None
    numero: Optional[str] = None
    valor_fatura: float
    descricao: Optional[str] = None
    linha: Optional[int] = None


class FaturaIn(BaseModel):
    transportadora: str = ""
    numero_fatura: str = ""
    competencia: Optional[str] = None
    arquivo: str = ""
    itens: List[FaturaItemIn]


class FaturaUpdateIn(BaseModel):
    status: Optional[str] = None
    observacoes: Optional[str] = None
    numero_fatura: Optional[str] = None
    competencia: Optional[str] = None
    transportadora: Optional[str] = None


@app.post("/api/conferencia/preview", summary="Ler uma fatura (Excel/CSV ou DOCCOB) e devolver as linhas detectadas, sem salvar")
async def conferencia_preview(file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Arquivo excede o limite de {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
        )
    if not content:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")
    try:
        # Ponto único: auto-detecta Excel/CSV vs DOCCOB (EDI PROCEDA).
        parsed = conferencia_mod.parse_auto(content, filename=file.filename or "")
    except conferencia_mod.FaturaParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"arquivo": file.filename, **parsed}


@app.post("/api/conferencia/preview-doccob", summary="Ler uma fatura DOCCOB (EDI PROCEDA) e devolver as linhas, sem salvar")
async def conferencia_preview_doccob(file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Arquivo excede o limite de {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
        )
    try:
        parsed = conferencia_mod.parse_doccob_bytes(content, filename=file.filename or "")
    except conferencia_mod.FaturaParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"arquivo": file.filename, **parsed}


@app.get("/api/conferencia/auditoria-aprovados", summary="Puxar da auditoria os CTes aprovados (status OK) p/ pré-montar uma fatura")
async def conferencia_auditoria_aprovados(transportadora: Optional[str] = None, mes: Optional[str] = None):
    return conferencia_mod.ctes_aprovados_auditoria(transportadora, mes)


@app.get("/api/conferencia/faturas", summary="Listar faturas conferidas (resumo recalculado, paginado)")
async def conferencia_list(limit: int = 100, offset: int = 0):
    todas = conferencia_mod.list_faturas()
    offset = max(0, offset)
    return {"total": len(todas), "faturas": todas[offset:offset + max(1, limit)]}


@app.get("/api/conferencia/alertas", summary="Riscos de pagamento indevido: CTe duplicado entre faturas e CTe órfão (sem auditoria)")
async def conferencia_alertas():
    return conferencia_mod.alertas_conferencia()


@app.post("/api/conferencia/faturas", summary="Salvar uma fatura conferida")
async def conferencia_create(body: FaturaIn):
    try:
        return conferencia_mod.create_fatura(
            body.transportadora, body.numero_fatura, body.competencia, body.arquivo,
            [it.dict() for it in body.itens],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/conferencia/faturas/{fatura_id}", summary="Detalhe da fatura com a conciliação linha a linha")
async def conferencia_get(fatura_id: int):
    fatura = conferencia_mod.get_fatura(fatura_id)
    if fatura is None:
        raise HTTPException(status_code=404, detail="Fatura não encontrada.")
    return fatura


@app.patch("/api/conferencia/faturas/{fatura_id}", summary="Atualizar cabeçalho/status/observações da fatura")
async def conferencia_update(fatura_id: int, body: FaturaUpdateIn):
    try:
        fatura = conferencia_mod.update_fatura(
            fatura_id, body.status, body.observacoes,
            body.numero_fatura, body.competencia, body.transportadora,
        )
    except conferencia_mod.AprovacaoBloqueada as e:
        # Trava: não aprovar/pagar fatura com CTe não importado ou não aprovado na auditoria.
        raise HTTPException(status_code=400, detail={"message": str(e), "blockers": e.blockers}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if fatura is None:
        raise HTTPException(status_code=404, detail="Fatura não encontrada.")
    return fatura


@app.delete("/api/conferencia/faturas/{fatura_id}", summary="Excluir uma fatura")
async def conferencia_delete(fatura_id: int):
    if not conferencia_mod.delete_fatura(fatura_id):
        raise HTTPException(status_code=404, detail="Fatura não encontrada.")
    return {"status": "OK"}


@app.get("/api/conferencia/faturas/{fatura_id}/export.xlsx", summary="Exportar a conferência da fatura em Excel")
async def conferencia_export_xlsx(fatura_id: int):
    fatura = conferencia_mod.get_fatura(fatura_id)
    if fatura is None:
        raise HTTPException(status_code=404, detail="Fatura não encontrada.")
    xlsx_bytes = conferencia_mod.export_xlsx(fatura)
    sufixo = "".join(c for c in (fatura.get("numero_fatura") or str(fatura_id)) if c.isalnum() or c in "-_")
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=conferencia_fatura_{sufixo}.xlsx"},
    )


@app.get("/api/conferencia/faturas/{fatura_id}/relatorio.pdf", summary="Relatório em PDF da fatura conferida/aprovada (nº, CTes, valores)")
async def conferencia_relatorio_pdf(fatura_id: int):
    fatura = conferencia_mod.get_fatura(fatura_id)
    if fatura is None:
        raise HTTPException(status_code=404, detail="Fatura não encontrada.")
    pdf_bytes = conferencia_mod.export_pdf(fatura)
    sufixo = "".join(c for c in (fatura.get("numero_fatura") or str(fatura_id)) if c.isalnum() or c in "-_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=relatorio_fatura_{sufixo}.pdf"},
    )


# ─── Home / Painel Executivo ─────────────────────────────────────────────────

@app.get("/api/home", summary="Resumo consolidado do Home (KPIs de todos os painéis)")
async def get_home(mes: Optional[str] = None):
    return dashboard_mod.resumo(mes)


@app.get("/api/home/meses", summary="Meses com dados (auditoria + entregas) para o filtro do Home")
async def get_home_meses():
    return dashboard_mod.list_meses()


# ─── Romaneio de expedição ───────────────────────────────────────────────────

class RomaneioCreateIn(BaseModel):
    transportadora: Optional[str] = ""
    data_expedicao: Optional[str] = ""
    veiculo: Optional[str] = ""
    placa: Optional[str] = ""
    motorista: Optional[str] = ""
    observacoes: Optional[str] = ""
    numero: Optional[str] = ""
    nota_ids: List[int] = []


class NotaIdsIn(BaseModel):
    nota_ids: List[int]


class RomaneioUpdateIn(BaseModel):
    situacao: Optional[str] = None
    observacoes: Optional[str] = None
    veiculo: Optional[str] = None
    placa: Optional[str] = None
    motorista: Optional[str] = None
    data_expedicao: Optional[str] = None
    transportadora: Optional[str] = None


async def _ler_uploads(files: List[UploadFile]) -> List[tuple]:
    saida = []
    for f in files:
        content = await f.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Arquivo {f.filename} excede o limite de {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
            )
        saida.append((f.filename or "", content))
    return saida


@app.post("/api/romaneio/upload-xml", summary="Subir XMLs de NF-e para a fila (sem vincular a romaneio)")
async def romaneio_upload_xml(files: List[UploadFile] = File(...)):
    uploads = await _ler_uploads(files)
    return romaneio_mod.add_notas_from_xml(uploads)


@app.post("/api/romaneio/import-pdf", summary="Importar romaneio pronto em PDF (Olist) — NFs vão para a fila")
async def romaneio_import_pdf(file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="Arquivo excede o limite permitido.")
    try:
        return romaneio_mod.import_pdf(content, filename=file.filename or "")
    except romaneio_mod.PdfParseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/romaneio/olist/sincronizar", summary="Puxar as NF-e emitidas no Olist para a fila do romaneio")
async def romaneio_olist_sincronizar():
    return olist_mod.sincronizar()


@app.get("/api/romaneio/olist/status", summary="Status da integração com o Olist (token, auto-sync, última sincronização)")
async def romaneio_olist_status():
    return olist_mod.status()


@app.post("/api/romaneio/olist/ressincronizar-peso", summary="Completar peso/volume das NFs já cadastradas relendo o XML no Olist")
async def romaneio_olist_ressincronizar_peso():
    return olist_mod.ressincronizar_peso_volume()


class AlertaResolverIn(BaseModel):
    nota_ids: Optional[List[int]] = None   # None = resolver todas as pendentes


@app.post("/api/romaneio/alertas-cancelamento/resolver", summary="Tirar do romaneio as NFs canceladas no Olist (limpa o alerta)")
async def romaneio_resolver_alertas(body: AlertaResolverIn):
    return romaneio_mod.resolver_alertas_cancelamento(body.nota_ids)


@app.post("/api/email/ler", summary="Ler a caixa de e-mail e baixar os anexos de EDI (CTe/OCOREN/DOCCOB)")
async def email_ler():
    return email_mod.ler_email()


@app.get("/api/email/status", summary="Status do carteiro de e-mail (config, auto, última leitura)")
async def email_status():
    return email_mod.status()


@app.post("/api/tracking/ssw-sync", summary="Sincronizar ocorrências via WebAPI de rastreamento da SSW (Fitlog/TG)")
async def tracking_ssw_sincronizar():
    return await ssw_sync_mod.sincronizar()


@app.get("/api/tracking/ssw-status", summary="Status do sincronizador SSW (config, auto, última sincronização)")
async def tracking_ssw_status():
    return ssw_sync_mod.status()


@app.post("/api/tracking/jamef-sync", summary="Buscar ocorrências acumuladas no recebedor de webhook da Jamef")
async def tracking_jamef_sincronizar():
    return await jamef_webhook_mod.sincronizar()


@app.get("/api/tracking/jamef-status", summary="Status do sincronizador do webhook da Jamef (config, auto, última sincronização)")
async def tracking_jamef_status():
    return jamef_webhook_mod.status()


@app.get("/api/olist/produtos", summary="Buscar produtos no Olist por nome ou SKU (traz o custo p/ o CMV)")
async def olist_produtos(q: str = ""):
    if not olist_mod.configurado():
        raise HTTPException(status_code=400, detail="Token do Olist não configurado no arquivo .env.")
    try:
        return {"produtos": olist_mod.pesquisar_produtos(q)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao consultar o Olist: {e}") from e


@app.get("/api/olist/produtos/{pid}/custo", summary="Custo real do produto pelas notas de entrada (kit = soma dos componentes)")
async def olist_produto_custo(pid: str):
    if not olist_mod.configurado():
        raise HTTPException(status_code=400, detail="Token do Olist não configurado no arquivo .env.")
    return olist_mod.custo_do_produto(pid)


@app.get("/api/olist/conjuntos", summary="Listar os conjuntos (kits) cadastrados no Olist para o seletor do comparativo")
async def olist_conjuntos(refresh: bool = False):
    if not olist_mod.configurado():
        raise HTTPException(status_code=400, detail="Token do Olist não configurado no arquivo .env.")
    try:
        return {"conjuntos": olist_mod.listar_conjuntos(force=refresh)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao consultar o Olist: {e}") from e


@app.get("/api/olist/conjuntos/{pid}/peso", summary="Peso de um conjunto: o maior entre bruto e cubado (m³ × cubagem)")
async def olist_conjunto_peso(pid: str, cubagem: float = 300.0):
    if not olist_mod.configurado():
        raise HTTPException(status_code=400, detail="Token do Olist não configurado no arquivo .env.")
    try:
        return olist_mod.peso_do_conjunto(pid, cubagem)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha ao consultar o Olist: {e}") from e


# ─── Análise de Vendas (Comercial) — Pedidos de Venda do Olist ───────────────

@app.get("/api/vendas", summary="Análise de vendas: KPIs, situações, faturamento por canal e evolução mensal (filtro por mês ou intervalo de/até)")
async def vendas_resumo(mes: Optional[str] = None, de: Optional[str] = None, ate: Optional[str] = None):
    return vendas_mod.resumo(mes, de, ate)


@app.get("/api/vendas/meses", summary="Meses com pedidos no cache local")
async def vendas_meses():
    return vendas_mod.list_meses()


@app.get("/api/vendas/produtos", summary="Produtos vendidos no período (paginado, por faturamento): quantidade, receita e análise de preço")
async def vendas_produtos(mes: Optional[str] = None, de: Optional[str] = None, ate: Optional[str] = None,
                          limit: int = 100, offset: int = 0, marca: Optional[str] = None):
    return {
        "total": vendas_mod.count_produtos(mes, de, ate, marca),
        "produtos": vendas_mod.produtos(mes, de, ate, limit, offset, marca),
    }


@app.get("/api/vendas/marcas", summary="Vendas por marca no período: quanto vendeu de cada marca (qtde, receita, participação)")
async def vendas_marcas(mes: Optional[str] = None, de: Optional[str] = None, ate: Optional[str] = None):
    return {"marcas": vendas_mod.vendas_por_marca(mes, de, ate)}


@app.get("/api/vendas/produto/{sku}/precos", summary="Histórico de preço de um produto (variação ao longo do tempo e por canal)")
async def vendas_produto_precos(sku: str, mes: Optional[str] = None, de: Optional[str] = None, ate: Optional[str] = None):
    return vendas_mod.preco_historico(sku, mes, de, ate)


@app.get("/api/vendas/status", summary="Status da integração de vendas (token, auto-sync, última sincronização, pedidos no cache)")
async def vendas_status():
    return vendas_mod.status()


@app.post("/api/vendas/sincronizar", summary="Puxar os pedidos de venda do Olist para o cache local (janela de meses configurada)")
async def vendas_sincronizar(force: bool = False):
    if not olist_mod.configurado():
        raise HTTPException(status_code=400, detail="Token do Olist não configurado no arquivo .env.")
    return vendas_mod.sincronizar(force=force)


@app.get("/api/vendas/comissoes", summary="Percentuais de comissão estimada por canal")
async def vendas_comissoes_get():
    return vendas_mod.load_comissoes()


class ComissoesIn(BaseModel):
    taxas: Dict[str, float]


@app.put("/api/vendas/comissoes", summary="Salvar percentuais de comissão estimada por canal")
async def vendas_comissoes_put(body: ComissoesIn):
    return {"status": "OK", "comissoes": vendas_mod.save_comissoes(body.taxas)}


@app.get("/api/vendas/metas", summary="Metas de faturamento mktplace por mês")
async def vendas_metas_get():
    return vendas_mod.load_metas()


class MetasIn(BaseModel):
    metas: Dict[str, Dict[str, float]]


@app.put("/api/vendas/metas", summary="Salvar metas de faturamento mktplace por mês (admin)")
async def vendas_metas_put(request: Request, body: MetasIn):
    _require_admin(request)
    return {"status": "OK", "metas": vendas_mod.save_metas(body.metas)}


class IgnorarIn(BaseModel):
    ids: List[str] = []
    clientes_contendo: List[str] = []


@app.get("/api/vendas/ignorados", summary="Regras de exclusão de pedidos de teste/homologação (por id e por cliente)")
async def vendas_ignorados_get():
    return vendas_mod.load_ignorados()


@app.put("/api/vendas/ignorados", summary="Salvar regras de exclusão e limpar do cache os pedidos que passam a ser ignorados")
async def vendas_ignorados_put(body: IgnorarIn):
    cfg = vendas_mod.save_ignorados({"ids": body.ids, "clientes_contendo": body.clientes_contendo})
    removidos = vendas_mod.purgar_ignorados()
    return {"status": "OK", "ignorar": cfg, "removidos_do_cache": removidos}


@app.get("/api/romaneio/fila", summary="NFs na fila (sem romaneio), agrupadas por transportadora")
async def romaneio_fila(transportadora: Optional[str] = None):
    return romaneio_mod.list_pool(transportadora)


@app.delete("/api/romaneio/fila/{nota_id}", summary="Descartar uma NF da fila")
async def romaneio_fila_delete(nota_id: int):
    if not romaneio_mod.delete_nota(nota_id):
        raise HTTPException(status_code=404, detail="NF não encontrada na fila (ou já vinculada a um romaneio).")
    return {"status": "OK"}


@app.post("/api/romaneio/romaneios", summary="Criar romaneio a partir das NFs da fila")
async def romaneio_create(body: RomaneioCreateIn):
    try:
        r = romaneio_mod.create_romaneio(body.dict(exclude={"nota_ids"}), body.nota_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return r


@app.post("/api/romaneio/romaneios/{rid}/add", summary="Adicionar NFs da fila a um romaneio")
async def romaneio_add_notas(rid: int, body: NotaIdsIn):
    r = romaneio_mod.add_notas(rid, body.nota_ids)
    if r is None:
        raise HTTPException(status_code=404, detail="Romaneio não encontrado.")
    return r


@app.post("/api/romaneio/romaneios/{rid}/remove", summary="Remover NFs do romaneio (voltam para a fila)")
async def romaneio_remove_notas(rid: int, body: NotaIdsIn):
    r = romaneio_mod.remove_notas(rid, body.nota_ids)
    if r is None:
        raise HTTPException(status_code=404, detail="Romaneio não encontrado.")
    return r


@app.post("/api/romaneio/romaneios/{rid}/mover", summary="Mover NFs (de outro romaneio ou da fila) para este romaneio, adotando a transportadora dele")
async def romaneio_mover_notas(rid: int, body: NotaIdsIn):
    r = romaneio_mod.mover_notas(body.nota_ids, rid)
    if r is None:
        raise HTTPException(status_code=404, detail="Romaneio de destino não encontrado (ou sem NFs).")
    return r


class NotaTransportadoraIn(BaseModel):
    transportadora: str
    cnpj: Optional[str] = ""


@app.post("/api/romaneio/fila/{nota_id}/transportadora", summary="Trocar a transportadora de uma NF que está na fila")
async def romaneio_set_transportadora(nota_id: int, body: NotaTransportadoraIn):
    if not romaneio_mod.set_transportadora_nota(nota_id, body.transportadora, body.cnpj or ""):
        raise HTTPException(status_code=404, detail="NF não encontrada na fila (ou já está em um romaneio).")
    return {"status": "OK"}


@app.post("/api/romaneio/romaneios/{rid}/liberar", summary="Liberar o romaneio (transportadora levou) e registrar na Torre, mesmo sem CTe")
async def romaneio_liberar(rid: int):
    # Calcula a previsão de prazo por NF (motor de cotação) p/ a Torre já ter data prevista.
    try:
        prazos = romaneio_mod.prazos_por_nota(rid, _precos, _prazos)
    except Exception:
        prazos = {}
    r = romaneio_mod.liberar_romaneio(rid, prazos)
    if r is None:
        raise HTTPException(status_code=404, detail="Romaneio não encontrado.")
    # Liberou → tenta recuperar OCOREN que chegou antes da liberação e ficou em espera.
    try:
        reproc = tracking.reprocessar_pendentes()
        if reproc:
            r["ocorrencias_reprocessadas"] = reproc
    except Exception:
        logger.warning("Falha ao reprocessar ocorrências pendentes após liberar romaneio.", exc_info=True)
    return r


@app.post("/api/romaneio/romaneios/{rid}/custo", summary="Calcular o custo de frete estimado do romaneio")
async def romaneio_custo(rid: int):
    res = romaneio_mod.calcular_custo(rid, _precos, _prazos)
    if res is None:
        raise HTTPException(status_code=404, detail="Romaneio não encontrado.")
    return res


@app.post("/api/romaneio/romaneios/{rid}/olist-expedicao", summary="Enviar o romaneio para a Expedição do Olist (cria o agrupamento)")
async def romaneio_olist_expedicao(rid: int):
    return olist_mod.enviar_romaneio_para_expedicao(rid)


@app.get("/api/romaneio/romaneios", summary="Listar romaneios (paginado)")
async def romaneio_list(transportadora: Optional[str] = None, situacao: Optional[str] = None,
                        mes: Optional[str] = None, search: Optional[str] = None,
                        limit: int = 100, offset: int = 0):
    # A função varre tudo (mantém o auto-encerramento global); aqui só recortamos
    # a página exibida e devolvemos o total para o pager.
    todos = romaneio_mod.list_romaneios(transportadora, situacao, mes, search)
    offset = max(0, offset)
    return {"total": len(todos), "romaneios": todos[offset:offset + max(1, limit)]}


@app.get("/api/romaneio/romaneios/{rid}", summary="Detalhe do romaneio (com cobertura de CTe)")
async def romaneio_get(rid: int):
    r = romaneio_mod.get_romaneio(rid)
    if r is None:
        raise HTTPException(status_code=404, detail="Romaneio não encontrado.")
    return r


@app.patch("/api/romaneio/romaneios/{rid}", summary="Atualizar situação/dados do romaneio")
async def romaneio_update(rid: int, body: RomaneioUpdateIn):
    campos = body.dict(exclude_none=True)
    try:
        # Setar 'liberado' por qualquer caminho (dropdown incluso) deve registrar as
        # NFs na Torre — senão o romaneio fica "liberado" mas some da torre de controle.
        if campos.get("situacao") == "liberado":
            outros = {k: v for k, v in campos.items() if k != "situacao"}
            if outros:
                romaneio_mod.update_romaneio(rid, outros)
            try:
                prazos = romaneio_mod.prazos_por_nota(rid, _precos, _prazos)
            except Exception:
                prazos = {}
            r = romaneio_mod.liberar_romaneio(rid, prazos)
        else:
            r = romaneio_mod.update_romaneio(rid, campos)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if r is None:
        raise HTTPException(status_code=404, detail="Romaneio não encontrado.")
    return r


@app.delete("/api/romaneio/romaneios/{rid}", summary="Excluir romaneio")
async def romaneio_delete(rid: int):
    if not romaneio_mod.delete_romaneio(rid):
        raise HTTPException(status_code=404, detail="Romaneio não encontrado.")
    return {"status": "OK"}


@app.get("/api/romaneio/romaneios/{rid}/pdf", summary="Gerar o romaneio impresso (PDF)")
async def romaneio_pdf(rid: int):
    pdf = romaneio_mod.gerar_pdf(rid)
    if pdf is None:
        raise HTTPException(status_code=404, detail="Romaneio não encontrado.")
    r = romaneio_mod.get_romaneio(rid)
    sufixo = "".join(c for c in (r.get("numero") or str(rid)) if c.isalnum() or c in "-_")
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=romaneio_{sufixo}.pdf"},
    )


@app.get("/api/romaneio/romaneios/{rid}/export.xlsx", summary="Exportar o romaneio em Excel")
async def romaneio_export_xlsx(rid: int):
    xlsx = romaneio_mod.export_xlsx(rid)
    if xlsx is None:
        raise HTTPException(status_code=404, detail="Romaneio não encontrado.")
    r = romaneio_mod.get_romaneio(rid)
    sufixo = "".join(c for c in (r.get("numero") or str(rid)) if c.isalnum() or c in "-_")
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=romaneio_{sufixo}.xlsx"},
    )


# ─── Coleta em Fornecedores ──────────────────────────────────────────────────

class ColetaIn(BaseModel):
    data_coleta: Optional[str] = ""
    fornecedor: Optional[str] = ""
    valor_nf: Optional[float] = 0
    valor_frete: Optional[float] = 0
    qtd_carretas: Optional[int] = 1
    transportadora: Optional[str] = ""
    origem: Optional[str] = ""
    destino: Optional[str] = ""
    previsao_entrega: Optional[str] = ""
    status: Optional[str] = ""
    observacoes: Optional[str] = ""
    notas: Optional[List[dict]] = None


@app.post("/api/coletas/importar-xml", summary="Ler VÁRIOS XMLs de NF-e e devolver as notas + cabeçalho da coleta (não grava)")
async def coletas_importar_xml(files: List[UploadFile] = File(...)):
    uploads = await _ler_uploads(files)
    return coletas_mod.dados_de_xmls(uploads)


@app.get("/api/coletas/export.xlsx", summary="Baixar relatório das coletas lançadas em Excel")
async def coletas_export(mes: Optional[str] = None, status: Optional[str] = None,
                         transportadora: Optional[str] = None, search: Optional[str] = None):
    xlsx = coletas_mod.export_xlsx(mes=mes, status=status, transportadora=transportadora, search=search)
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=coletas.xlsx"},
    )


@app.get("/api/coletas", summary="Coletas em fornecedores: linhas (paginadas) + KPIs + séries dos gráficos")
async def coletas_list(mes: Optional[str] = None, status: Optional[str] = None,
                       transportadora: Optional[str] = None, search: Optional[str] = None,
                       limit: int = 100, offset: int = 0):
    return coletas_mod.painel(mes=mes, status=status, transportadora=transportadora,
                              search=search, limit=limit, offset=offset)


@app.post("/api/coletas", summary="Registrar uma coleta em fornecedor")
async def coletas_create(body: ColetaIn):
    return coletas_mod.create_coleta(body.dict())


@app.put("/api/coletas/{cid}", summary="Atualizar uma coleta")
async def coletas_update(cid: int, body: ColetaIn):
    c = coletas_mod.update_coleta(cid, body.dict(exclude_unset=True))
    if c is None:
        raise HTTPException(status_code=404, detail="Coleta não encontrada.")
    return c


@app.delete("/api/coletas/{cid}", summary="Excluir uma coleta")
async def coletas_delete(cid: int):
    if not coletas_mod.delete_coleta(cid):
        raise HTTPException(status_code=404, detail="Coleta não encontrada.")
    return {"ok": True}
