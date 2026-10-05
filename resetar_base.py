"""
Reset para produção — limpa os dados TRANSACIONAIS de teste/demonstração e deixa
o sistema pronto para receber os dados reais.

O QUE APAGA (dados de movimento):
  - audit_history.db : auditorias de CTe, entregas da torre, NFs e ocorrências
  - conferencia.db   : faturas conferidas e seus itens
  - romaneio.db      : romaneios de expedição e suas notas

O QUE PRESERVA (configuração):
  - cadastro.db      : base de acordo — transportadoras + tabelas de preço/prazo
  - auth.db          : usuários e senhas do login
  - Excel/JSON em tables/ e o mapeamento de CNPJs

Opcionalmente (--arquivos) move os XMLs de CTe e os EDIs JÁ PROCESSADOS para uma
pasta de arquivo, para a entrada começar limpa.

Uso:
    python resetar_base.py                    # DRY-RUN: só mostra o que seria apagado
    python resetar_base.py --confirmar        # executa a limpeza dos bancos
    python resetar_base.py --confirmar --arquivos   # também arquiva XMLs/EDIs processados
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from app import history, conferencia, romaneio  # usa os mesmos DB_PATH dos módulos

RAIZ = Path(__file__).parent

# (rótulo, caminho do banco, tabelas em ordem segura p/ apagar — filhas antes das mães)
ALVOS = [
    ("Auditoria + Torre de Controle", history.DB_PATH,
     ["ocorrencias", "entrega_nfs", "entregas", "audit_history"]),
    ("Conferência de Fatura", conferencia.DB_PATH,
     ["fatura_itens", "faturas"]),
    ("Romaneio de Expedição", romaneio.DB_PATH,
     ["notas", "romaneios"]),
]

# Pastas de arquivos processados (dados de teste em disco).
PASTAS_ARQUIVOS = [
    RAIZ / "cte_xml" / "processados" / "ok",
    RAIZ / "cte_xml" / "processados" / "nok",
    RAIZ / "edi" / "processados",
]


def _contar(db_path: str, tabela: str) -> int | None:
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
    except sqlite3.OperationalError:
        return None  # tabela ainda não existe
    finally:
        conn.close()


def _limpar(db_path: str, tabelas: list[str]) -> int:
    conn = sqlite3.connect(db_path)
    total = 0
    try:
        for t in tabelas:
            try:
                cur = conn.execute(f"DELETE FROM {t}")
                total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except sqlite3.OperationalError:
                pass
        # zera os contadores de AUTOINCREMENT quando existirem
        try:
            conn.execute("DELETE FROM sqlite_sequence")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return total


def _arquivar_arquivos(executar: bool) -> None:
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    destino_base = RAIZ / "_arquivo_teste" / carimbo
    for pasta in PASTAS_ARQUIVOS:
        if not pasta.exists():
            continue
        arquivos = [p for p in pasta.iterdir() if p.is_file() and p.name != ".gitkeep"]
        if not arquivos:
            continue
        rel = pasta.relative_to(RAIZ)
        print(f"  {rel}: {len(arquivos)} arquivo(s)"
              + (f" -> _arquivo_teste/{carimbo}/{rel}" if executar else " (seriam arquivados)"))
        if executar:
            destino = destino_base / rel
            destino.mkdir(parents=True, exist_ok=True)
            for p in arquivos:
                shutil.move(str(p), str(destino / p.name))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # evita erro no console Windows (cp1252) com emojis/acentos
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Reset para produção do Freight Gateway.")
    ap.add_argument("--confirmar", action="store_true", help="Executa de fato (sem isso é só dry-run).")
    ap.add_argument("--arquivos", action="store_true", help="Também arquiva XMLs/EDIs processados.")
    args = ap.parse_args()

    executar = args.confirmar
    print("=" * 68)
    print("RESET PARA PRODUÇÃO — Freight Gateway" + ("" if executar else "   [DRY-RUN]"))
    print("=" * 68)

    print("\nDados TRANSACIONAIS que serão apagados:")
    algum = False
    for rotulo, db, tabelas in ALVOS:
        contagens = {t: _contar(db, t) for t in tabelas}
        existe = any(v is not None for v in contagens.values())
        resumo = ", ".join(f"{t}={v}" for t, v in contagens.items() if v is not None) or "(banco/tabelas ainda não criados)"
        print(f"  • {rotulo}: {resumo}")
        if existe and any((v or 0) > 0 for v in contagens.values()):
            algum = True

    if args.arquivos:
        print("\nArquivos processados em disco:")
        _arquivar_arquivos(executar=False if not executar else True)

    print("\nSerá PRESERVADO: base de acordo (cadastro.db), usuários/login (auth.db),")
    print("planilhas e JSONs de configuração em tables/.")

    if not executar:
        print("\n" + "-" * 68)
        if algum:
            print("DRY-RUN. Nada foi apagado. Para executar de verdade:")
            print("    python resetar_base.py --confirmar" + (" --arquivos" if args.arquivos else ""))
        else:
            print("Não há dados transacionais para apagar — a base já está limpa.")
        return 0

    print("\nApagando...")
    for rotulo, db, tabelas in ALVOS:
        if not Path(db).exists():
            continue
        _limpar(db, tabelas)
        print(f"  • {rotulo}: limpo.")
    print("\n✅ Bancos transacionais zerados. O sistema está pronto para os dados reais.")
    print("   Reinicie o servidor (ABRIR-PAINEL-FRETE.bat) para recarregar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
