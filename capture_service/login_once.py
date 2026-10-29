"""
Roda uma vez, LOCALMENTE (fora do Docker, num navegador de verdade), para
gerar o storage_state.json que o capture_service reusa em toda automacao.

Por que assim, e nao usuario/senha direto no container: o login da Microsoft
provavelmente pede MFA, que nao da pra automatizar (nem seria correto tentar
contornar). Fazendo o login manual uma vez e salvando a sessao autenticada
(cookies + local storage), o container so reusa essa sessao, sem nunca ver a
senha nem precisar repetir o MFA a cada captura. Quando a sessao expirar (o
capture_service comecar a devolver a tela de login em vez do dashboard),
repita este passo.

Uso (na sua maquina, fora do Docker):
    pip install playwright
    playwright install chromium
    python login_once.py https://app.powerbi.com/... (link do dashboard publicado)

Se o login da Microsoft ficar carregando sem fim no Chromium de teste, use o
Microsoft Edge instalado na maquina (nao precisa baixar nada):
    python login_once.py https://app.powerbi.com/... --edge
"""

import os
import sys

from playwright.sync_api import sync_playwright


def main(url: str, canal=None) -> None:
    with sync_playwright() as p:
        # Sem a marca de automacao, o login da Microsoft se comporta como num navegador
        # comum (em automacao ele as vezes fica carregando sem fim ou abre popup em branco).
        browser = p.chromium.launch(
            headless=False,
            channel=canal,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)
        input(
            "Faca o login no navegador aberto (usuario, senha, MFA se pedir) e "
            "espere o dashboard carregar. Depois volte aqui e pressione Enter..."
        )
        context.storage_state(path="storage_state.json")
        browser.close()
    print(f"Gerado {os.path.abspath('storage_state.json')} -- ele precisa ficar em capture_service/")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or any(a.startswith("--") and a != "--edge" for a in sys.argv[1:]):
        print("Uso: python login_once.py <url-do-dashboard-publicado> [--edge]")
        sys.exit(1)
    main(args[0], "msedge" if "--edge" in sys.argv[1:] else None)
