"""Lista os grupos de WhatsApp que a instância do Evolution API enxerga.

Serve para descobrir o identificador (JID) de um grupo antes de cadastrá-lo como
destinatário (reporting.destinatario.whatsapp). O número da automação precisa já
estar dentro do grupo: peça a um administrador dele que o adicione.

Roda na VM, onde o Evolution API responde em localhost:8080. A chave vem do .env
(EVOLUTION_API_KEY) ou do ambiente. Não precisa de nada além do Python.

  python scripts/listar_grupos.py [--nome texto] [--instancia pafil-reporting] [--url http://localhost:8080]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # sem python-dotenv, a chave precisa estar no ambiente

INSTANCIA_PADRAO = "pafil-reporting"
URL_PADRAO = "http://localhost:8080"


def normalizar(texto: str) -> str:
    """Minúsculas e sem acento, para `--nome comunicacao` achar "Comunicação"."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c)).casefold()


def buscar_grupos(url: str, instancia: str, chave: str) -> list[dict]:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/group/fetchAllGroups/{instancia}?getParticipants=false",
        headers={"apikey": chave},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        corpo = json.load(resp)
    if not isinstance(corpo, list):
        raise ValueError(f"resposta inesperada do Evolution API: {str(corpo)[:300]}")
    return corpo


def main() -> int:
    ap = argparse.ArgumentParser(description="Lista os grupos de WhatsApp da instância.")
    ap.add_argument("--nome", help="só os grupos cujo nome contém este texto (sem diferenciar maiúsculas nem acentos)")
    ap.add_argument("--instancia", default=INSTANCIA_PADRAO)
    ap.add_argument("--url", default=os.getenv("EVOLUTION_URL", URL_PADRAO))
    args = ap.parse_args()

    # Nome de grupo costuma ter emoji, que o console do Windows (cp1252) não imprime.
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(errors="replace")

    chave = os.getenv("EVOLUTION_API_KEY")
    if not chave:
        raise SystemExit("EVOLUTION_API_KEY não definida (veja o .env.example)")

    try:
        grupos = buscar_grupos(args.url, args.instancia, chave)
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", "replace")[:300]
        print(f"O Evolution API respondeu {exc.code}: {detalhe}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Não consegui falar com {args.url}: {exc}. Rode na VM, onde ele responde.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.nome:
        alvo = normalizar(args.nome)
        grupos = [g for g in grupos if alvo in normalizar(g.get("subject") or "")]
    grupos.sort(key=lambda g: normalizar(g.get("subject") or ""))

    if not grupos:
        print("Nenhum grupo encontrado. O número precisa estar dentro do grupo.")
        return 0

    print("identificador\tmembros\tnome")
    for g in grupos:
        print(f"{g.get('id')}\t{g.get('size', '?')}\t{g.get('subject')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
