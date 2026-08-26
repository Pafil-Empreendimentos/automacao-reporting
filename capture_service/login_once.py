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
"""

import sys

from playwright.sync_api import sync_playwright


def main(url: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)
        input(
            "Faca o login no navegador aberto (usuario, senha, MFA se pedir) e "
            "espere o dashboard carregar. Depois volte aqui e pressione Enter..."
        )
        context.storage_state(path="storage_state.json")
        browser.close()
    print("Gerado storage_state.json -- copie para automacao_reporting/capture_service/")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python login_once.py <url-do-dashboard-publicado>")
        sys.exit(1)
    main(sys.argv[1])
