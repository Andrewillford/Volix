"""
Limpeza de painéis — zera os NÚMEROS DE MOVIMENTO, mas preserva o que interessa.

PRESERVA:
  - Coleta em Fornecedores (coletas.db)
  - a(s) NF(s) PENDENTE(s) na fila do Romaneio (notas ainda SEM romaneio)
  - cadastro de frete / prazo / transportadoras (cadastro.db)
  - usuários e senhas do login (auth.db)

APAGA:
  - audit_history.db : Auditoria de CTe, Torre de Controle, %NF, Ocorrências, Scorecard
  - conferencia.db   : Conferência de Fatura (faturas + itens)
  - romaneio.db      : romaneios JÁ CRIADOS + as notas já expedidas
                       (a fila pendente — notas sem romaneio — é MANTIDA)

Antes de apagar, faz um backup de TODOS os bancos em
backups/pre-limpeza_<carimbo>/ (a pasta backups/ é ignorada pelo Git).

Uso:
    python limpar_paineis.py                 # DRY-RUN: só mostra o que faria
    python limpar_paineis.py --confirmar     # executa (com backup automático)

Para replicar o mesmo estado em outro computador: faça `git pull`, rode este
script lá com --confirmar e reinicie o LogHub.
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
TABLES = RAIZ / "tables"


def _contar(db_path: str, sql: str) -> int | None:
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _estado() -> dict:
    return {
        "Auditoria CTe": _contar(history.DB_PATH, "SELECT COUNT(*) FROM audit_history"),
        "Torre (entregas)": _contar(history.DB_PATH, "SELECT COUNT(*) FROM entregas"),
        "NFs de entrega": _contar(history.DB_PATH, "SELECT COUNT(*) FROM entrega_nfs"),
        "Ocorrências": _contar(history.DB_PATH, "SELECT COUNT(*) FROM ocorrencias"),
        "Conferência (faturas)": _contar(conferencia.DB_PATH, "SELECT COUNT(*) FROM faturas"),
        "Romaneios criados": _contar(romaneio.DB_PATH, "SELECT COUNT(*) FROM romaneios"),
        "NFs já expedidas": _contar(romaneio.DB_PATH, "SELECT COUNT(*) FROM notas WHERE romaneio_id IS NOT NULL"),
        "NFs PENDENTES (mantidas)": _contar(romaneio.DB_PATH, "SELECT COUNT(*) FROM notas WHERE romaneio_id IS NULL"),
    }


def _backup() -> Path:
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    destino = RAIZ / "backups" / f"pre-limpeza_{carimbo}"
    destino.mkdir(parents=True, exist_ok=True)
    for f in list(TABLES.glob("*.db")) + list(TABLES.glob("*.db-wal")) + list(TABLES.glob("*.db-shm")):
        shutil.copy2(f, destino)
    return destino


def _exec(db_path: str, comandos: list[str]) -> None:
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        for sql in comandos:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()


def _limpar() -> None:
    # Auditoria + Torre (filhas antes das mães)
    if Path(history.DB_PATH).exists():
        _exec(history.DB_PATH, ["DELETE FROM ocorrencias", "DELETE FROM entrega_nfs",
                                "DELETE FROM entregas", "DELETE FROM audit_history"])
    # Conferência
    if Path(conferencia.DB_PATH).exists():
        _exec(conferencia.DB_PATH, ["DELETE FROM fatura_itens", "DELETE FROM faturas"])
    # Romaneio: apaga romaneios criados e notas JÁ expedidas; mantém a fila pendente
    if Path(romaneio.DB_PATH).exists():
        _exec(romaneio.DB_PATH, ["DELETE FROM romaneio_itens",
                                 "DELETE FROM notas WHERE romaneio_id IS NOT NULL",
                                 "DELETE FROM romaneios"])


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Limpa os números dos painéis, preservando coletas, NF pendente e cadastro.")
    ap.add_argument("--confirmar", action="store_true", help="Executa de fato (sem isso é só dry-run).")
    args = ap.parse_args()

    print("=" * 70)
    print("LIMPEZA DE PAINÉIS — LogHub" + ("" if args.confirmar else "   [DRY-RUN]"))
    print("=" * 70)

    est = _estado()
    print("\nEstado atual:")
    for k, v in est.items():
        print(f"  • {k}: {'—' if v is None else v}")

    print("\nAPAGA: Auditoria CTe, Torre, %NF, Ocorrências, Conferência e romaneios criados.")
    print("MANTÉM: Coletas, NF(s) pendente(s) na fila, cadastro de frete/prazo e login.")

    if not args.confirmar:
        print("\n" + "-" * 70)
        print("DRY-RUN. Nada foi alterado. Para executar de verdade (com backup automático):")
        print("    python limpar_paineis.py --confirmar")
        return 0

    print("\nFazendo backup dos bancos...")
    destino = _backup()
    print(f"  backup em: {destino.relative_to(RAIZ)}")

    print("Apagando...")
    _limpar()

    print("\nEstado final:")
    for k, v in _estado().items():
        print(f"  • {k}: {'—' if v is None else v}")

    print("\n✅ Painéis zerados. Coletas, NF pendente e cadastro preservados.")
    print("   Reinicie o LogHub (INICIAR-LOGHUB.vbs) para recarregar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
