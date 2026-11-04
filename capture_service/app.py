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

# So para o erro 409 contar como esta cada botao da janela (visivel, ativo, tamanho, o que o cobre) e a
# propria janela, quando o clique em Fechar nao funciona.
JS_DESCRICAO = """els => els.map(e => {
  const r = e.getBoundingClientRect();
  const cs = getComputedStyle(e);
  const topo = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  const coberto = topo && topo !== e && !e.contains(topo) ? ', coberto por ' + topo.tagName + '.' + String(topo.className).slice(0, 30) : '';
  const rotulo = (e.getAttribute('aria-label') || e.innerText || e.title || '').trim().slice(0, 30);
  const visivel = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  const ativo = !e.disabled && e.getAttribute('aria-disabled') !== 'true';
  return rotulo + ' (' + (visivel ? 'visivel' : 'oculto') + ', ' + (ativo ? 'ativo' : 'desativado') + ', ' + Math.round(r.width) + 'x' + Math.round(r.height) + coberto + ')';
})"""
JS_JANELA = "e => e.tagName + ' role=' + e.getAttribute('role') + ' modal=' + e.getAttribute('aria-modal') + (e.open ? ' open' : '') + (e.hasAttribute('inert') ? ' inert' : '')"

# Controles da janela alem dos botoes (caixa de marcar, campo, link): o que pode estar segurando o Fechar.
JS_CONTROLES = """el => [...el.querySelectorAll('input, select, textarea, a[href], [role="checkbox"], [role="radio"], mat-checkbox')]
  .slice(0, 8)
  .map(c => c.tagName + (c.type ? ':' + c.type : '') + (c.checked ? ' marcado' : '') + (c.disabled ? ' desativado' : '') + ' ' + (c.innerText || c.getAttribute('aria-label') || '').trim().slice(0, 40))"""

# Ultimo recurso: esconde a janela e o fundo escuro dela (camadas do overlay do Angular CDK, que o Power BI usa).
JS_OCULTAR = """el => {
  const painel = el.closest('.cdk-overlay-pane') || el;
  const camada = painel.closest('.cdk-global-overlay-wrapper') || painel;
  const alvos = [camada];
  const raiz = camada.parentElement;
  if (raiz) raiz.querySelectorAll('.cdk-overlay-backdrop').forEach(f => alvos.push(f));
  alvos.forEach(a => a.style.setProperty('display', 'none', 'important'));
  return alvos.length;
}"""

# Depois de esconder: o que do overlay ainda recebe o clique em pontos da pagina (se sobrar, o print sairia escurecido).
JS_CAMADA_SOBRANDO = """() => {
  const sobras = [];
  for (const [fx, fy] of [[0.05, 0.05], [0.5, 0.05], [0.95, 0.05], [0.05, 0.5], [0.5, 0.5], [0.95, 0.5], [0.05, 0.95], [0.5, 0.95], [0.95, 0.95]]) {
    const el = document.elementFromPoint(innerWidth * fx, innerHeight * fy);
    const camada = el && el.closest('.cdk-overlay-backdrop, .cdk-overlay-pane, .cdk-global-overlay-wrapper');
    if (camada) sobras.push(camada.tagName + '.' + String(camada.className).slice(0, 30));
  }
  return sobras;
}"""

# Vai no fim do erro 409, para saber qual versao do codigo esta rodando no container.
VERSAO_DO_CODIGO = "janela-avaliacao-v5"

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
    """Janela, botoes e rotulos da janela de avaliacao que esta aberta, ou None se ela nao esta na tela.

    O texto e conferido aqui, em Python, e nao com filter(has_text=regex): o Playwright avalia a
    regex no navegador, onde o curinga de letra so pega ASCII, e a janela real nunca era achada.
    """
    janelas = page.locator(SELETOR_JANELAS)
    for i, texto in enumerate(janelas.all_inner_texts()):
        if PADRAO_AVISO_DE_AVALIACAO.search(texto):
            janela = janelas.nth(i)
            botoes = janela.locator(SELETOR_BOTOES)
            return janela, botoes, rotulos_dos_botoes(botoes)
    return None


def escolher_botao_que_fecha(botoes, rotulos):
    """Indice do botao que fecha: o primeiro, na ordem de BOTOES_QUE_FECHAM, que esta visivel e ativo.

    Se nenhum estiver, o primeiro com o rotulo certo (quem chama espera e tenta). None se nao houver.
    """
    candidatos = [i for padrao in BOTOES_QUE_FECHAM for i, rotulo in enumerate(rotulos) if padrao.search(rotulo)]
    for i in candidatos:
        try:
            if botoes.nth(i).is_visible() and botoes.nth(i).is_enabled():
                return i
        except Exception:  # o botao sumiu no meio: tenta o proximo
            continue
    return candidatos[0] if candidatos else None


def aguardar_botao(page, botoes, i, ms) -> bool:
    """Espera ate `ms` o botao ficar visivel e ativo: um aviso pode liberar o Fechar so depois de uns segundos."""
    esperou = 0
    while True:
        try:
            if botoes.nth(i).is_visible() and botoes.nth(i).is_enabled():
                return True
        except Exception:  # o botao sumiu no meio
            return False
        if esperou >= ms:
            return False
        page.wait_for_timeout(500)
        esperou += 500


def motivo_do_erro(erro) -> str:
    """Ultimas linhas do erro do Playwright: o call log diz se o botao esta desativado, oculto ou coberto."""
    linhas = [" ".join(linha.split()) for linha in str(erro).splitlines() if linha.strip()]
    return " / ".join(linha[:120] for linha in linhas[-3:])


def descrever_botoes(botoes) -> str:
    """Como cada botao esta na tela (visivel, ativo, tamanho, o que o cobre), so para o erro contar."""
    try:
        return ", ".join(botoes.evaluate_all(JS_DESCRICAO))
    except Exception:  # so diagnostico: nao pode esconder o erro que ja vai ser devolvido
        return ""


def descrever_janela(janela) -> str:
    try:
        return str(janela.evaluate(JS_JANELA, timeout=2000))
    except Exception:  # so diagnostico
        return ""


def dispensar_aviso_de_avaliacao(page, tentativas: int = 3, passos=None, espera_ms: int = 6000) -> None:
    """Fecha a janela "Suas avaliacoes gratuitas do Microsoft Fabric e do Power BI terminam em N dias".

    E so um aviso, mas fica por cima do relatorio e o guarda de janelas de capture() recusaria o
    print. O botao e escolhido dentro da propria janela, para nao fechar um painel do relatorio que
    tenha o mesmo nome, e so entre os que fecham (BOTOES_QUE_FECHAM), nunca os de compra ou de criar
    conta. Os botoes sao achados por CSS e pelo rotulo, e nao por get_by_role, que ignora o que o
    Power BI marca como oculto para leitores de tela. Prefere o botao visivel e ativo e, na primeira
    tentativa, espera ate `espera_ms` ele ficar assim; se nao ficar, nao clica (o clique so esperaria
    a toa) e tenta o clique do proprio elemento. Da segunda tentativa em diante aperta Escape tambem.
    `passos` recebe o que foi tentado e por que o clique falhou, para o erro de quem chama contar a
    historia. Se a janela nao sair, quem chama tenta ocultar_aviso_de_avaliacao e, por fim, recusa.
    """
    passos = passos if passos is not None else []
    dispensou = False
    for tentativa in range(1, tentativas + 1):
        try:
            achado = achar_aviso_de_avaliacao(page)
            if achado is not None and tentativa > 1:
                # O clique da tentativa anterior nao tirou a janela: Escape tambem.
                page.keyboard.press("Escape")
                passos.append(f"{tentativa}: Escape")
                page.wait_for_timeout(700)
                achado = achar_aviso_de_avaliacao(page)
        except Exception as erro:  # a pagina pode ter mudado no meio da leitura; capture() confere de novo
            passos.append(f"{tentativa}: nao deu para ler a janela ({type(erro).__name__})")
            break
        if achado is None:
            if tentativa > 1:
                passos.append(f"{tentativa}: a janela saiu")
            break
        janela, botoes, rotulos = achado
        alvo = escolher_botao_que_fecha(botoes, rotulos)
        if alvo is None:
            passos.append(f"{tentativa}: nenhum botao que fecha, Escape")
            page.keyboard.press("Escape")
        else:
            nome = rotulos[alvo]
            clicou = False
            # So na primeira tentativa espera o botao liberar: se nao liberou, nao vai liberar.
            if aguardar_botao(page, botoes, alvo, espera_ms if tentativa == 1 else 0):
                try:
                    botoes.nth(alvo).click(timeout=2000)
                    passos.append(f"{tentativa}: clique em {nome} ok")
                    clicou = True
                except Exception as erro:  # coberto por outra coisa: o motivo vem no call log do Playwright
                    passos.append(f"{tentativa}: clique em {nome} falhou ({type(erro).__name__}: {motivo_do_erro(erro)})")
            else:
                passos.append(f"{tentativa}: {nome} nao esta visivel e ativo")
            if not clicou:
                if tentativa == 1:
                    passos.append(f"botoes: {descrever_botoes(botoes)}; janela: {descrever_janela(janela)}")
                try:
                    botoes.nth(alvo).evaluate("el => el.click()", timeout=2000)
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


def ocultar_aviso_de_avaliacao(page, passos) -> str:
    """Ultimo recurso: esconde so para o print a janela de avaliacao e o fundo escuro dela.

    Em 04/nov/2026 o Fechar dessa janela vinha desativado no navegador do servico e nada o liberava
    (nem espera, nem Escape). A janela so informa, entao esconder as camadas dela (a janela e o fundo
    escuro do overlay do Angular CDK) nao mexe no relatorio nem na conta. Depois confere que a janela
    saiu e que nenhuma camada do overlay ainda recebe o clique em pontos da pagina; se sobrar algo, o
    print sairia escurecido, e quem chama recusa. Devolve "nada" (a janela nao esta na tela),
    "ocultada" ou "falhou".
    """
    try:
        achado = achar_aviso_de_avaliacao(page)
        if achado is None:
            return "nada"
        janela = achado[0]
        passos.append(f"controles: {', '.join(janela.evaluate(JS_CONTROLES, timeout=2000)) or 'nenhum'}")
        texto = " ".join(janela.inner_text(timeout=2000).split())[:600]
        escondidas = janela.evaluate(JS_OCULTAR, timeout=2000)
        page.wait_for_timeout(500)
        sobras = page.evaluate(JS_CAMADA_SOBRANDO)
        ainda_aberta = achar_aviso_de_avaliacao(page) is not None
    except Exception as erro:  # so um recurso: se falhar, capture() recusa o print
        passos.append(f"ocultar: nao deu ({type(erro).__name__})")
        return "falhou"
    if ainda_aberta or sobras:
        motivo = "a janela continua na tela" if ainda_aberta else f"ainda ha overlay na pagina ({', '.join(sobras[:3])})"
        passos.append(f"ocultar: {escondidas} camada(s) escondida(s), mas {motivo}")
        passos.append(f"texto da janela: {texto}")
        return "falhou"
    passos.append(f"ocultar: {escondidas} camada(s) escondida(s)")
    return "ocultada"


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
            for rodada in range(2):
                dispensar_aviso_de_identidade(page)
                dispensar_aviso_de_avaliacao(page, passos=passos, espera_ms=6000 if rodada == 0 else 0)
            # Se o Fechar da janela de avaliacao vem desativado e nada o libera, o ultimo recurso e esconder
            # a janela e o fundo escuro dela so para o print, conferindo que nada escureceu a pagina.
            escondeu = ocultar_aviso_de_avaliacao(page, passos)
            avisos = "janela de avaliacao ocultada" if escondeu == "ocultada" else ("janela de avaliacao fechada" if passos else "")
            if PADRAO_REAUTENTICACAO.search(page.inner_text("body")):
                raise HTTPException(
                    status_code=401,
                    detail="sessao do Power BI pede nova verificacao de identidade e o aviso nao saiu com Cancelar -- rode login_once.py de novo e substitua o storage_state.json",
                )
            dialogos = page.locator(SELETOR_JANELAS).all_inner_texts()
            # "falhou" sem janela na tela: ela foi escondida, mas algo do overlay ainda cobre a pagina.
            if dialogos or escondeu == "falhou":
                resumo = (
                    " ".join(dialogos[0].split())[:200]
                    if dialogos
                    else "a janela de avaliacao foi escondida, mas o overlay ainda cobre a pagina"
                )
                tentou = f" | tentou: {'; '.join(passos)}"[:1800] if passos else ""
                outras = " // ".join(" ".join(texto.split())[:80] for texto in dialogos[1:4])
                outras = f" | outras janelas: {outras}" if outras else ""
                raise HTTPException(
                    status_code=409,
                    detail=f"janela aberta sobre o relatorio: {resumo}{botoes_da_janela(page)}{outras}{tentou} [{VERSAO_DO_CODIGO}]",
                )
            png_bytes = page.screenshot(full_page=False)
        finally:
            browser.close()

    return Response(content=png_bytes, media_type="image/png", headers={"x-avisos": avisos} if avisos else None)
