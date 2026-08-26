# Automação de reporting (WhatsApp + email)

Envia periodicamente, sem intervenção manual, o print de dashboards
publicados no Power BI Service para uma lista de destinatários, por WhatsApp
e por email. Decisão de arquitetura (26/ago/2026): **nenhuma assinatura ou
licença nova**, tudo self-hosted e open source, rodando na mesma máquina de
produção do Postgres (ver `infra/RUNBOOK_WINDOWS.md`), sem tocar nesse banco.

## Componentes

| Peça | O que faz | Como roda |
|---|---|---|
| [`docker-compose.yml`](docker-compose.yml) | Sobe os três serviços abaixo juntos | Docker Engine, dentro do WSL2 |
| n8n | Orquestra o fluxo (gatilho → captura → condição → envio) | container `n8nio/n8n` |
| `capture_service/` | Tira o print do dashboard publicado (Playwright) | container próprio, construído a partir do `Dockerfile` desta pasta |
| Evolution API | Envia a mensagem de WhatsApp | container `atendai/evolution-api`, com um Postgres próprio e isolado (`evolution-db`), separado do banco de produção do Pafil |

O envio de email não tem serviço próprio: usa o node nativo "Microsoft
Outlook" do n8n, autenticado via Graph API (OAuth2), sem precisar de SMTP.
Ver seção 4.

**Por que Docker Engine, e não Docker Desktop:** o Docker Desktop tem termos
de licença comercial dependendo do porte da empresa. Docker Engine (o motor
em si, sem a interface gráfica) é open source de verdade e roda dentro de uma
distro Linux via WSL2, sem esse risco.

## 1. Pré-requisitos na máquina de produção

A máquina está limpa de Node.js/npm e Docker (confirmado em 26/ago/2026), ou
seja, os passos abaixo partem do zero. Rode num PowerShell elevado:

```powershell
wsl --install -d Ubuntu
```

Isso ativa o WSL2 e instala uma distro Ubuntu. Reinicie a máquina se pedido.
Depois, abra o Ubuntu (menu Iniciar ou `wsl` no PowerShell) e, já dentro dele
(shell bash), instale o Docker Engine pelo script oficial:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

Feche e reabra o terminal do Ubuntu para o grupo `docker` valer, e confirme:

```bash
docker --version
docker compose version
```

O WSL2 não roda serviços automaticamente no boot do Windows por padrão. Duas
formas de garantir que o Docker suba sozinho depois de um reboot da máquina:

- Se a distro suportar systemd (Ubuntu recente no WSL2 já suporta): edite
  `/etc/wsl.conf` dentro do Ubuntu, adicione:
  ```
  [boot]
  systemd=true
  ```
  e reinicie o WSL (`wsl --shutdown` no PowerShell, depois abra o Ubuntu de
  novo). Com systemd ativo, o `dockerd` sobe junto do boot da distro.
- Alternativa mais simples de garantir que a distro (e o Docker dentro dela)
  suba no logon da máquina: uma tarefa agendada rodando `wsl -d Ubuntu -e true`
  no logon do Windows já é suficiente para o WSL2 iniciar a distro.

## 2. Subindo a stack

Dentro do Ubuntu (WSL2), no caminho do repositório clonado:

```bash
cd automacao_reporting
cp .env.example .env
# edite o .env com senhas/chaves reais antes de continuar
docker compose up -d
```

Confira que os três containers subiram:

```bash
docker compose ps
```

O n8n fica acessível em `http://localhost:5678` (do próprio Windows também,
já que o WSL2 encaminha portas para o host automaticamente). Faça login com
o usuário/senha definidos em `N8N_BASIC_AUTH_USER`/`N8N_BASIC_AUTH_PASSWORD`.

## 3. Gerando a sessão autenticada do Power BI

O `capture_service` não faz login sozinho (a conta tem MFA, e não seria
correto tentar automatizar isso). Em vez disso, ele reusa uma sessão já
autenticada, gerada uma vez à mão:

```bash
pip install playwright
playwright install chromium
python capture_service/login_once.py "https://app.powerbi.com/<link-do-dashboard-publicado>"
```

Faça o login normalmente (usuário, senha, MFA) na janela que abrir, espere o
dashboard carregar, e volte ao terminal para confirmar. Isso gera
`capture_service/storage_state.json`, que o `docker-compose.yml` já monta
dentro do container. Repita este passo sempre que o `capture_service` voltar
a devolver a tela de login em vez do print (a sessão expira periodicamente).

Teste o serviço:

```bash
curl -X POST http://localhost:8000/capture \
  -H "x-api-key: <CAPTURE_API_KEY do .env>" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://app.powerbi.com/<link-do-dashboard-publicado>"}' \
  --output teste.png
```

## 4. Emparelhando o WhatsApp (Evolution API)

O endpoint de QR code da API (`/instance/connect`) é reportado como instável
em algumas versões do Evolution API (às vezes não devolve o QR mesmo com a
instância criada). Por isso o `docker-compose.yml` inclui o **Evolution
Manager**, a interface web oficial só para esse pareamento inicial:

1. Abra `http://localhost:3000` (ou pelo IP da máquina, se acessando de fora
   dela).
2. Na primeira tela, informe a URL da API (`http://localhost:8080`, ou
   `http://evolution-api:8080` se o Manager pedir o nome interno do serviço)
   e a `EVOLUTION_API_KEY` do `.env`.
3. Crie uma instância nova pela interface.
4. Abra o QR code exibido e escaneie com o WhatsApp do número que vai enviar
   os reportings (Configurações → Aparelhos conectados).

Depois de parear, o Manager não faz mais parte do fluxo — mandar mensagem é
uma chamada HTTP simples direto na API (porta 8080), que o n8n faz por um
node "HTTP Request" apontando para o endpoint de envio da instância. Confira
a documentação oficial do Evolution API para o endpoint exato, já que nomes
podem mudar entre versões.

## 5. Configurando o envio de email (Outlook, sem SMTP)

Contas Microsoft 365 costumam ter a autenticação básica de SMTP desativada
por padrão desde 2022 — por isso o envio usa o node nativo "Microsoft
Outlook" do n8n (fala com a API do Graph via OAuth2), em vez de SMTP direto.
Isso não exige licença nova: só um App Registration no Azure AD, gratuito no
tenant que a Pafil já tem.

**No portal do Azure (portal.azure.com):**

1. Vá em **Microsoft Entra ID → Registros de aplicativo → Novo registro**.
2. Nome: algo como `n8n-reporting-automacao`. Tipo de conta: **Contas somente
   neste diretório organizacional (Single tenant)**.
3. Em **URI de redirecionamento**, deixe em branco por enquanto — o valor
   exato vem do n8n no passo seguinte, então volte aqui depois de criar a
   credencial no n8n.
4. Anote o **Application (client) ID** e o **Directory (tenant) ID**, que
   aparecem na página de visão geral do app recém-criado.
5. Em **Certificados e segredos → Novo segredo do cliente**, crie um e copie
   o **valor** imediatamente (ele só aparece uma vez).
6. Em **Permissões de API → Adicionar uma permissão → Microsoft Graph →
   Permissões delegadas**, adicione `Mail.Send`, `offline_access` e
   `User.Read`. Se o tenant exigir consentimento de administrador, clique em
   **Conceder consentimento do administrador** (pode exigir alguém da TI com
   papel de Global Admin — mesmo padrão de pedido do `infra/PEDIDO_TI.md`).

**No n8n:**

1. Em **Credentials → New → Microsoft Outlook OAuth2 API**, cole o Client ID,
   o Client Secret e o Tenant ID.
2. O n8n mostra, na própria tela da credencial, a **Redirect URL** exata que
   ele espera. Copie esse valor e cole de volta no campo **URI de
   redirecionamento** do App Registration (passo 3 acima).
3. Clique em **Connect my account** e complete o consentimento (login da
   conta que vai efetivamente enviar os emails).
4. No node "Microsoft Outlook" do workflow, use a operação **Send**.

## 6. Próximos passos

Com os três serviços de pé e as duas credenciais (Power BI e Outlook)
configuradas, falta montar o workflow em si dentro do n8n:

1. Gatilho: schedule (após o horário usual do refresh) ou polling na API do
   Power BI checando o status do último refresh do dataset.
2. Loop sobre a tabela de configuração de distribuição (dashboard, página,
   destinatário, canal, condição) — ainda por criar no Postgres de produção.
3. Chamada ao `capture_service` (`POST /capture`) por dashboard.
4. Nó de condição avaliando a regra de cada destinatário.
5. Fan-out: HTTP Request para o Evolution API (WhatsApp) e node "Microsoft
   Outlook" (email).

A tabela de configuração e o workflow em si ainda não existem — este runbook
cobre só a infraestrutura de base.
