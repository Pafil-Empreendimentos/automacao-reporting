import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from playwright.sync_api import sync_playwright
from pydantic import BaseModel

API_KEY = os.environ["CAPTURE_API_KEY"]
STORAGE_STATE_PATH = Path("/app/storage_state.json")

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
        png_bytes = page.screenshot(full_page=False)
        browser.close()

    return Response(content=png_bytes, media_type="image/png")
