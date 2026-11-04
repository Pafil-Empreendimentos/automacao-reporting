import os
import re
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

# A sessao salva pode ser boa o bastante para o relatorio abrir e, mesmo assim, a
# Microsoft pedir nova verificacao de identidade (MFA). A pagina continua em
# app.powerbi.com, entao a checagem de redirecionamento acima nao pega e o print
# sairia com a janela de aviso por cima. O texto da janela (pt-BR e en) denuncia.
PADRAO_REAUTENTICACAO = re.compile(
    r"verificar sua identidade|verify your identity|autentique novamente|sign in again",
    re.IGNORECASE,
)

# Janelas que o Power BI abre por cima do relatorio.
SELETOR_JANELAS = '[role="dialog"]:visible, [role="alertdialog"]:visible, [aria-modal="true"]:visible'

# Botoes de uma janela, e o rotulo de cada um (aria-label, texto ou title, o que vier primeiro).
SELETOR_BOTOES = 'button, [role="button"]'
JS_ROTULOS = "els => els.map(e => e.getAttribute('aria-label') || e.innerText || e.title || '')"

# Vai no fim do erro 409, para saber qual versao do codigo esta rodando no container.
VERSAO_DO_CODIGO = "janela-avaliacao-v3"

# Enquanto a avaliacao gratuita do Microsoft Fabric / Power BI esta para acabar, o Power BI abre a cada
# sessao uma janela "Suas avaliacoes gratuitas ... terminam em N dias" por cima do relatorio. Ela so
# informa e nao pede nada, entao da para fechar; qualquer outra janela continua barrando o print.
# O texto e conferido em Python, de proposito: no navegador (filter has_text) a regex nao pega letra
# acentuada, e "avaliacoes" aparece com cedilha e til.
PADRAO_AVISO_DE_AVALIACAO = re.compile(
    r"avalia\S{1,10}\s+gratuita|free\s+trial|trial\W+(ends?|expires?)",
    re.IGNORECASE,
)

# So botoes que fecham. Nunca os de compra, upgrade ou continuar, que abririam outro fluxo.
BOTOES_QUE_FECHAM = (
    re.compile(r"^(fechar|close)$", re.IGNORECASE),
    re.compile(r"^(agora n[a\xe3]o|not now)$", re.IGNORECASE),
    re.compile(r"^(dispensar|dismiss)$", re.IGNORECASE),
    re.compile(r"^(entendi|got it)$", re.IGNORECASE),
)

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


def dispensar_aviso_de_identidade(page, tentativas: int = 4) -> None:
    """Fecha o aviso "Voce precisa verificar sua identidade".

    O Power BI mostra esse aviso em sessoes abertas por navegador automatizado, as vezes
    em fila (duas vezes seguidas), mesmo com o relatorio ja carregado e com dados. Cancelar
    so recusa a reverificacao. O aviso e dispensado, nunca confirmado: confirmar abriria
    o login com MFA. Se ele nao sair, quem chama recusa o print.
    """
    dispensou = False
    for _ in range(tentativas):
        if not PADRAO_REAUTENTICACAO.search(page.inner_text("body")):
            break
        fechou = False
        for botao in (
            page.get_by_role("button", name=re.compile(r"^(cancelar|cancel)$", re.IGNORECASE)),
            page.get_by_role("button", name=re.compile(r"^(fechar|close)$", re.IGNORECASE)),
        ):
            try:
                botao.first.click(timeout=2000)
                fechou = True
                break
            except Exception:  # sem esse botao na tela: tenta o proximo
                continue
        if not fechou:
            page.keyboard.press("Escape")
        dispensou = True
        # Da tempo de o segundo aviso, se houver, aparecer.
        page.wait_for_timeout(1500)
    if dispensou:
        # E de os visuais se refazerem depois que o aviso sai.
        page.wait_for_timeout(2000)


def rotulos_dos_botoes(botoes) -> list:
    """Rotulo de cada botao do localizador, na ordem do DOM ("" quando o botao nao tem nome)."""
    return [" ".join((rotulo or "").split()) for rotulo in botoes.evaluate_all(JS_ROTULOS)]


def achar_aviso_de_avaliacao(page):
    """Botoes e rotulos da janela de avaliacao que esta aberta, ou None se ela nao esta na tela.

    O texto e conferido aqui, em Python, e nao com filter(has_text=regex): o Playwright avalia a
    regex no navegador, onde o curinga de letra so pega ASCII, e a janela real nunca era achada.
    """
    janelas = page.locator(SELETOR_JANELAS)
    for i, texto in enumerate(janelas.all_inner_texts()):
        if PADRAO_AVISO_DE_AVALIACAO.search(texto):
            botoes = janelas.nth(i).locator(SELETOR_BOTOES)
            return botoes, rotulos_dos_botoes(botoes)
    return None


def dispensar_aviso_de_avaliacao(page, tentativas: int = 3, passos=None) -> None:
    """Fecha a janela "Suas avaliacoes gratuitas do Microsoft Fabric e do Power BI terminam em N dias".

    E so um aviso, mas fica por cima do relatorio e o guarda de janelas de capture() recusaria o
    print. O botao e escolhido dentro da propria janela, para nao fechar um painel do relatorio que
    tenha o mesmo nome, e so entre os que fecham (BOTOES_QUE_FECHAM), nunca os de compra ou de criar
    conta. Os botoes sao achados por CSS e pelo rotulo, e nao por get_by_role, que ignora o que o
    Power BI marca como oculto para leitores de tela. Se o clique falhar, tenta o clique do proprio
    elemento; sem botao assim, tenta Escape. `passos` recebe o que foi tentado, para o erro de quem
    chama contar a historia. Se a janela nao sair, quem chama recusa o print.
    """
    passos = passos if passos is not None else []
    dispensou = False
    for tentativa in range(1, tentativas + 1):
        try:
            achado = achar_aviso_de_avaliacao(page)
        except Exception as erro:  # a pagina pode ter mudado no meio da leitura; capture() confere de novo
            passos.append(f"{tentativa}: nao deu para ler a janela ({type(erro).__name__})")
            break
        if achado is None:
            if tentativa > 1:
                passos.append(f"{tentativa}: a janela saiu")
            break
        botoes, rotulos = achado
        alvo = next(
            (i for padrao in BOTOES_QUE_FECHAM for i, rotulo in enumerate(rotulos) if padrao.search(rotulo)),
            None,
        )
        if alvo is None:
            passos.append(f"{tentativa}: nenhum botao que fecha, Escape")
            page.keyboard.press("Escape")
        else:
            nome = rotulos[alvo]
            try:
                botoes.nth(alvo).click(timeout=2000)
                passos.append(f"{tentativa}: clique em {nome} ok")
            except Exception as erro:  # coberto por outra coisa, ou sem tamanho: clica pelo proprio elemento
                passos.append(f"{tentativa}: clique em {nome} falhou ({type(erro).__name__})")
                try:
                    botoes.nth(alvo).evaluate("el => el.click()")
                    passos.append(f"{tentativa}: clique do DOM em {nome} ok")
                except Exception as erro_dom:
                    passos.append(f"{tentativa}: clique do DOM em {nome} falhou ({type(erro_dom).__name__}), Escape")
                    page.keyboard.press("Escape")
        dispensou = True
        # Da tempo de a janela sair e de outro aviso, se houver, aparecer.
        page.wait_for_timeout(1500)
    if dispensou:
        # E de os visuais se refazerem depois que a janela sai.
        page.wait_for_timeout(2000)


def botoes_da_janela(page) -> str:
    """Nomes dos botoes da primeira janela aberta, para o erro dizer o que dava para fechar."""
    try:
        nomes = [r for r in rotulos_dos_botoes(page.locator(SELETOR_JANELAS).first.locator(SELETOR_BOTOES)) if r]
    except Exception:  # so diagnostico: nao pode esconder o erro que ja vai ser devolvido
        return ""
    return f" | botoes: {', '.join(nomes)}" if nomes else ""


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
            # Os avisos de reautenticacao (com Cancelar) e de fim de avaliacao gratuita (com Fechar
            # ou Escape) sao dispensados. Um pode vir depois do outro, entao duas rodadas. Se algum
            # sobrar, ou se houver qualquer outra janela por cima do relatorio, melhor falhar e
            # avisar do que mandar para a diretoria um print com a janela.
            passos = []
            for _ in range(2):
                dispensar_aviso_de_identidade(page)
                dispensar_aviso_de_avaliacao(page, passos=passos)
            if PADRAO_REAUTENTICACAO.search(page.inner_text("body")):
                raise HTTPException(
                    status_code=401,
                    detail="sessao do Power BI pede nova verificacao de identidade e o aviso nao saiu com Cancelar -- rode login_once.py de novo e substitua o storage_state.json",
                )
            dialogos = page.locator(SELETOR_JANELAS).all_inner_texts()
            if dialogos:
                resumo = " ".join(dialogos[0].split())[:200]
                tentou = f" | tentou: {'; '.join(passos)}"[:600] if passos else ""
                raise HTTPException(
                    status_code=409,
                    detail=f"janela aberta sobre o relatorio: {resumo}{botoes_da_janela(page)}{tentou} [{VERSAO_DO_CODIGO}]",
                )
            png_bytes = page.screenshot(full_page=False)
        finally:
            browser.close()

    return Response(content=png_bytes, media_type="image/png")
