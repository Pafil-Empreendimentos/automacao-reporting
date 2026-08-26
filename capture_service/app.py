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
    wait_ms: int = 6000
    width: int = 1920
    height: int = 1080


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
        page.goto(req.url, wait_until="networkidle")
        # Dashboards do Power BI Service seguem renderizando visuais depois do
        # "networkidle" (chamadas assincronas do motor de DAX). O wait_ms cobre
        # essa folga -- ajuste pelo tempo real do dashboard mais pesado.
        page.wait_for_timeout(req.wait_ms)
        png_bytes = page.screenshot(full_page=False)
        browser.close()

    return Response(content=png_bytes, media_type="image/png")
