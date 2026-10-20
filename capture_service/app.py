import os
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from playwright.sync_api import sync_playwright
from pydantic import BaseModel

API_KEY = os.environ["CAPTURE_API_KEY"]
STORAGE_STATE_PATH = Path(os.environ.get("STORAGE_STATE_PATH", "/app/storage_state.json"))

# Quando a sessao do Power BI expira, o servico nao recebe erro nenhum: a
# Microsoft redireciona para a tela de login e o screenshot sai perfeito, so que
# da pagina errada. Sem esta checagem, essa imagem seguiria para o WhatsApp da
# diretoria como se fosse o dashboard.
HOSTS_DE_LOGIN = {
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.live.com",
}

app = FastAPI()


class CaptureRequest(BaseModel):
    url: str
    wait_ms: int = 10000
    # Mais largo que 1920 de proposito: o painel de navegacao lateral do Power
    # BI Service (paginas + rail de icones) consome uns 350px fixos, entao um
    # relatorio desenhado para 1920 de largura fica cortado a direita se a
    # viewport for exatamente 1920.
    width: int = 2400
    height: int = 1350


def caiu_no_login(url_final: str) -> bool:
    return (urlparse(url_final).hostname or "") in HOSTS_DE_LOGIN


@app.post("/capture")
def capture(req: CaptureRequest, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="chave invalida")
    if not STORAGE_STATE_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail="storage_state.json ausente -- rode login_once.py e monte o arquivo no container",
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(
                storage_state=str(STORAGE_STATE_PATH),
                viewport={"width": req.width, "height": req.height},
            )
            page = context.new_page()
            # "networkidle" nao serve aqui: o Power BI Service mantem conexoes de
            # fundo (websocket, telemetria) o tempo todo, entao a pagina nunca fica
            # ociosa e o goto sempre estoura por timeout. "load" espera so o
            # carregamento inicial: o wait_ms abaixo cobre o resto da renderizacao
            # dos visuais, que continua acontecendo depois via chamadas de DAX.
            page.goto(req.url, wait_until="load", timeout=60000)
            page.wait_for_timeout(req.wait_ms)
            if caiu_no_login(page.url):
                raise HTTPException(
                    status_code=401,
                    detail="sessao do Power BI expirada -- rode login_once.py de novo e substitua o storage_state.json",
                )
            png_bytes = page.screenshot(full_page=False)
        finally:
            browser.close()

    return Response(content=png_bytes, media_type="image/png")
