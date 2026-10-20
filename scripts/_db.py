"""Conexão com o Postgres e logging, compartilhados pelos scripts desta pasta.

Lê as variáveis PG_* do ambiente (ou do .env na raiz do repositório). O banco é o
mesmo da plataforma de dados (data-hub-pafil): o schema `integracao` mora ao lado
de bronze, silver e gold.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import psycopg
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent

load_dotenv(RAIZ / ".env")


@dataclass(frozen=True)
class ConfigPostgres:
    host: str
    port: int
    db: str
    user: str
    password: str
    sslmode: str

    def conninfo(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.db} "
            f"user={self.user} password={self.password} sslmode={self.sslmode}"
        )


def _obrigatoria(nome: str) -> str:
    valor = os.getenv(nome)
    if not valor:
        raise SystemExit(f"Variável de ambiente obrigatória não definida: {nome} (veja o .env.example)")
    return valor


def carregar_config_pg() -> ConfigPostgres:
    return ConfigPostgres(
        host=_obrigatoria("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        db=_obrigatoria("PG_DB"),
        user=_obrigatoria("PG_USER"),
        password=_obrigatoria("PG_PASSWORD"),
        sslmode=os.getenv("PG_SSLMODE", "require"),
    )


@contextmanager
def conectar(cfg: ConfigPostgres) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(cfg.conninfo(), autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def aplicar_ddl(conn: psycopg.Connection, caminho_sql: str) -> None:
    """Executa um arquivo .sql inteiro (todo o script é idempotente)."""
    with open(caminho_sql, "r", encoding="utf-8-sig") as arquivo:
        ddl = arquivo.read()
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def configurar_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def get_logger(nome: str) -> logging.Logger:
    return logging.getLogger(nome)
