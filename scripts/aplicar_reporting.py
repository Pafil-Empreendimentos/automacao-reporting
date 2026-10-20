"""Aplica o schema `reporting` (configuração da automação de envio de dashboards).

Independente das camadas do data-hub-pafil: não lê nem escreve em bronze/silver/gold,
só cria o metadado operacional que a stack deste repositorio consome.
Pode rodar antes ou depois de qualquer carga do data-hub-pafil.

  python scripts/aplicar_reporting.py [--validar]
"""
from __future__ import annotations

import argparse

from _db import RAIZ, aplicar_ddl, carregar_config_pg, conectar, configurar_logging, get_logger

log = get_logger("aplicar_reporting")

REPORTING_SQL = RAIZ / "sql" / "reporting.sql"

OBJETOS = [
    "dashboard", "destinatario", "distribuicao", "envio_log", "v_fila_envio",
    "alerta_log", "v_falhas_do_dia",
]


def validar(conn) -> int:
    falhas = 0
    with conn.cursor() as cur:
        for o in OBJETOS:
            try:
                cur.execute(f"SELECT count(*) FROM reporting.{o}")
                log.info("  reporting.%-16s OK  (%s linhas)", o, cur.fetchone()[0])
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                log.error("  reporting.%-16s FALHOU: %s", o, str(exc).splitlines()[0])
                falhas += 1
    return falhas


def main() -> int:
    ap = argparse.ArgumentParser(description="Aplica o schema reporting.")
    ap.add_argument("--validar", action="store_true", help="só valida (não aplica DDL)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    configurar_logging(args.verbose)
    cfg = carregar_config_pg()

    with conectar(cfg) as conn:
        if not args.validar:
            aplicar_ddl(conn, str(REPORTING_SQL))
            log.info("Schema reporting aplicado (%s).", REPORTING_SQL.name)
        log.info("Validação smoke:")
        falhas = validar(conn)

    if falhas:
        log.error("%d objeto(s) do schema reporting com erro.", falhas)
        return 1
    log.info("Reporting OK. Próximo passo: sql/grants_reporting.sql")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
