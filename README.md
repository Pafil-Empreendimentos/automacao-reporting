# Automação de reporting (WhatsApp + email)

Envia periodicamente, sem intervenção manual, o print de dashboards
publicados no Power BI Service para uma lista de destinatários, por WhatsApp
e por email. Decisão de arquitetura (26/ago/2026): **nenhuma assinatura ou
licença nova**, tudo self-hosted e open source.

**Onde roda:** na mesma VM Windows que já hospeda o Postgres de produção, a
ingestão diária do CVDW e o gateway do Power BI. A stack fica em Docker
Engine dentro do WSL2, em `/mnt/c/pafil/app/automacao_reporting`.

Estar na mesma VM **não** significa estar na mesma rede. Este é o ponto que
mais confunde e a causa provável da primeira falha:

```
                        VM Windows
  ┌──────────────────────────────┬──────────────────────────────┐
  │  Windows (nativo)            │  WSL2 → Docker Engine        │
  │                              │                              │
  │  Postgres pafil_dw  :5432    │   n8n ── capture_service     │
  │  ingestão diária (serviço)   │    │                         │
  │  gateway do Power BI         │    └── Evolution API         │
  │                              │         + evolution-db       │
  └──────────────────────────────┴──────────────────────────────┘
            ▲                                   │
            └───────────────────────────────────┘
              pelo IP da VM na LAN, não por localhost
              (ver seção 3 — são dois `localhost` diferentes)
```

## Componentes

| Peça | O que faz | Como roda |
|---|---|---|
| [`docker-compose.yml`](docker-compose.yml) | Sobe os serviços abaixo juntos | Docker Engine, no WSL2 da VM |
| n8n | Orquestra o fluxo (gatilho → captura → condição → envio) | container `n8nio/n8n` |
| [`capture_service/`](capture_service/) | Tira o print do dashboard publicado (Playwright) | container próprio, construído a partir do `Dockerfile` desta pasta |
| Evolution API | Envia a mensagem de WhatsApp | container `evoapicloud/evolution-api`, com um Postgres próprio e isolado (`evolution-db`), separado do banco de produção do Pafil |
| [`sql/reporting.sql`](sql/reporting.sql) | Schema `reporting`: quem recebe o quê, quando e por qual canal | aplicado no Postgres da VM por `aplicar_reporting.py` |
| [`workflow_n8n.json`](workflow_n8n.json) | O fluxo de envio, pronto para importar no n8n | importado pela interface do n8n |
| [`workflow_alerta_n8n.json`](workflow_alerta_n8n.json) | Avisa os administradores quando um envio do dia não saiu dentro da janela | importado pela interface do n8n |

O envio de email não tem serviço próprio: usa o node nativo "Microsoft
Outlook" do n8n, autenticado via Graph API (OAuth2), sem precisar de SMTP.
Ver seção 6.

## 1. Estado atual e ordem de go-live

Os quatro containers já sobem e ficam de pé (confirmado em 27/ago/2026). O
envio em si nunca rodou ponta a ponta na VM, mas o workflow foi exercitado num
n8n local com Postgres, `capture_service` e Evolution API de mentira (ver a
seção 10 para o que esse teste cobre e o que não cobre).

A ordem abaixo é a que evita voltar atrás. Cada passo aponta para a seção que o
detalha:

1. **Notebook:** commitar e dar push nos arquivos desta pasta e do
   `aplicar_reporting.py`, para a VM conseguir puxá-los.
2. **VM, PowerShell:** `git pull` (sempre pelo PowerShell, nunca pelo git do
   WSL: os dois têm `core.autocrlf` diferentes e cada um vê o outro como
   "mudança local").
3. **VM, PowerShell:** criar o schema e o role (seção 7).
4. **VM, WSL:** reconstruir o `capture_service`, que ganhou a detecção de sessão
   expirada (abaixo).
5. **VM:** ligar o n8n ao Postgres (seção 3), se ainda não estiver feito.
6. **VM:** gerar a sessão do Power BI (seção 4).
7. **n8n:** criar as três credenciais e importar os dois workflows (seção 8).
8. **Banco:** cadastrar um dashboard e **você mesmo** como destinatário, com
   `recebe_alertas = true` (seção 7).
9. **n8n:** `Execute Workflow` manual e conferir o WhatsApp e o `envio_log`.
10. **n8n:** só então ligar o toggle **Active** nos dois workflows.
11. **Depois, sem bloquear nada:** o email (seção 6). O envio por WhatsApp
    funciona sem ele.

Duas conferências que valem antes de seguir:

**O `capture_service` precisa estar na versão corrigida.** A primeira versão
usava `wait_until="networkidle"`, que no Power BI Service **nunca** resolve —
o site mantém websocket e telemetria abertos, então a página jamais fica
ociosa e a captura sempre estoura por timeout:

```bash
grep -n "wait_until" capture_service/app.py
```

Tem que responder `wait_until="load"`. Se responder `networkidle`, atualize o
arquivo e reconstrua a imagem — o `Dockerfile` copia o `app.py` para dentro
dela, então editar no disco não basta:

```bash
docker compose up -d --build capture-service
```

**O `capture_service` agora recusa a tela de login.** Quando a sessão do Power
BI expira, a Microsoft redireciona para o login e o print sai perfeito, só que
da página errada: sem tratamento, essa imagem seguiria para a diretoria como se
fosse o dashboard. A versão atual confere o endereço final da página e, se caiu
no login, responde `401 sessao do Power BI expirada`, que o workflow registra
no `envio_log` e o alerta (seção 8) transforma em aviso. Reconstrua a imagem
para valer, o mesmo comando acima.

**A `N8N_ENCRYPTION_KEY` precisa estar fixa no `.env`.** É com ela que o n8n
criptografa as credenciais salvas (Postgres, Outlook, as duas de header). Sem
a variável, ele gera uma sozinho e guarda dentro do volume — e no dia em que o
volume for recriado, todas as credenciais viram lixo indecifrável e têm que
ser redigitadas, inclusive o consentimento OAuth do Outlook. Como o n8n já
rodou, **não invente uma chave nova**: leia a que ele gerou e registre no
`.env`, senão ele passa a reclamar de chave divergente.

```bash
docker compose exec n8n cat /home/node/.n8n/config
```

## 2. Sobre o ambiente (referência)

Já está montado; esta seção existe para quando a VM for recriada ou alguém
precisar entender por que as escolhas foram estas.

Docker Engine dentro do WSL2, e **não** Docker Desktop: o Desktop exige
assinatura paga para empresas acima de 250 funcionários ou US$ 10M de
faturamento, o que contrariaria a decisão de não adicionar licença nenhuma.

Numa VM, o WSL2 depende de **virtualização aninhada** — ele é ele próprio uma
máquina virtual, então o hipervisor precisa expor as extensões de
virtualização ao convidado. Sem isso o `wsl --install` falha ou a distro sobe
e morre, com mensagem que não aponta para a causa. No Hyper-V se resolve com
`Set-VMProcessor -VMName <vm> -ExposeVirtualizationExtensions $true`, com a VM
desligada.

**Para a stack sobreviver a um reboot da VM**, duas peças, porque o WSL2 não
sobe sozinho no boot do Windows:

1. systemd dentro da distro, para o `dockerd` subir junto dela — `/etc/wsl.conf`
   com `[boot]` / `systemd=true`, seguido de `wsl --shutdown` no PowerShell.
2. Uma tarefa agendada no Windows, disparando no logon e rodando
   `wsl -d Ubuntu -e true` — só isso já basta para iniciar a distro.

> A stack roda a partir de `/mnt/c/pafil/app/`, e funciona. Se um dia o bind
> mount do `storage_state.json` começar a dar problema de permissão ou
> lentidão, a causa é a ponte entre Windows e WSL, e a saída é mover a pasta
> para dentro do sistema de arquivos do WSL (`~/pafil`).

## 3. Ligando o n8n ao Postgres

O Postgres está na mesma VM, mas do lado Windows, escutando em
`localhost:5432`. Os containers vivem dentro do WSL2, que tem pilha de rede
própria. São **dois `localhost` diferentes**, e `host.docker.internal:host-gateway`
não resolve isso: ele aponta para a VM do WSL, não para o Windows.

O caminho é o n8n falar com o Postgres pelo **IP da VM na rede da empresa**.
Poderia ser o IP interno do WSL, mas ele muda a cada reinício da distro — e
uma automação que para depois de todo reboot, sem erro visível, é pior do que
uma que nunca funcionou.

### Descubra os dois endereços

No PowerShell da VM, o IP dela na LAN (chamaremos de `<IP-VM>`):

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback|WSL' } | Select-Object IPAddress, InterfaceAlias
```

No WSL, o IP da distro (chamaremos de `<IP-WSL>`, normalmente `172.x.x.x`):

```bash
ip -4 addr show eth0 | grep -oP 'inet \K[\d.]+'
```

### Mudança 1 — o Postgres passa a escutar no IP da VM

Edite `conf.d\postgresql-pafil.conf` dentro do diretório de dados do Postgres
(tipicamente `C:\Program Files\PostgreSQL\16\data`). Nunca o `postgresql.conf`
principal — mantê-lo intocado deixa o drop-in ser o único diff em relação ao
padrão do instalador.

```
listen_addresses = 'localhost,<IP-VM>'
```

Explicitar o IP em vez de usar `'*'` é proposital: `'*'` abriria a porta em
toda interface, inclusive as que ninguém previu.

### Mudança 2 — o `pg_hba.conf` libera a sub-rede do WSL

No mesmo diretório de dados, adicione **uma** linha. O endereço de origem que
o Postgres enxerga não é o do container: o Docker faz NAT para o IP da distro,
então quem chega é `<IP-WSL>`. Como esse IP muda, libere a faixa privada que o
contém (confira que o `<IP-WSL>` que você achou cai nela):

```
host    pafil_dw    pafil_n8n    172.16.0.0/12    scram-sha-256
```

Essa linha já existe comentada nos dois scripts de provisionamento, e isso é
de propósito: o `provisionar_postgres.ps1` reescreve o `pg_hba.conf` inteiro,
então uma linha adicionada só à mão desaparece na próxima execução dele e o
envio para em silêncio. Descomente lá também.

### Mudança 3 — o firewall do Windows aceita a conexão

```powershell
New-NetFirewallRule -DisplayName "Postgres <- WSL (n8n)" -Direction Inbound -Protocol TCP -LocalPort 5432 -RemoteAddress 172.16.0.0/12 -Action Allow -Profile Any
```

O `-Profile Any` não é preguiça: o Windows costuma classificar o adaptador
`vEthernet (WSL)` como rede **Pública**, o perfil mais restrito. Uma regra
criada só para o perfil Privado é aceita sem reclamar e não funciona — é um
dos jeitos mais chatos de perder tempo aqui.

### Aplicar e verificar

`listen_addresses` só vale depois de reiniciar (não basta recarregar):

```powershell
Restart-Service postgresql-x64-16
```

Verifique em **três saltos**, nesta ordem. Cada um isola uma das três
mudanças, então o primeiro que falhar já diz onde está o problema.

1. O Postgres está escutando no IP certo? (PowerShell da VM — tem que aparecer
   o `<IP-VM>`, não só `127.0.0.1`)
   ```powershell
   Get-NetTCPConnection -LocalPort 5432 -State Listen | Select-Object LocalAddress
   ```
2. O WSL alcança? (se falhar aqui, é firewall ou `listen_addresses`)
   ```bash
   nc -vz <IP-VM> 5432
   ```
3. Um container alcança? (se o salto 2 passou e este não, é rede do Docker)
   ```bash
   docker run --rm alpine nc -zv <IP-VM> 5432
   ```

### Aponte a stack para lá

No `.env` desta pasta:

```
PG_HOST_IP=<IP-VM>
```

O `docker-compose.yml` transforma isso no apelido `pafil-db` dentro do
container do n8n, e é esse nome que vai na credencial. A vantagem é que
mudança de IP se resolve numa linha do `.env`, sem reabrir a credencial no
n8n para redigitar a senha. Recrie o container para o apelido valer:

```bash
docker compose up -d
```

## 4. Gerando a sessão autenticada do Power BI

O `capture_service` não faz login sozinho (a conta tem MFA, e não seria
correto tentar automatizar isso). Em vez disso, ele reusa uma sessão já
autenticada, gerada uma vez à mão — e como a VM tem tela, dá para fazer isso
nela mesma, num PowerShell:

```powershell
pip install playwright ; playwright install chromium
```

```powershell
python C:\pafil\app\automacao_reporting\capture_service\login_once.py "<url-do-dashboard>"
```

Faça o login normalmente (usuário, senha, MFA) na janela que abrir, espere o
dashboard carregar, e volte ao terminal para confirmar. Isso gera
`storage_state.json` ao lado do script, que é exatamente onde o
`docker-compose.yml` espera encontrá-lo.

Repita este passo sempre que o `capture_service` responder `401 sessao do Power
BI expirada` (a sessão expira periodicamente), e depois reinicie o container
(`docker compose restart capture-service`): o arquivo entra no container por
bind mount, e trocar o arquivo no disco nem sempre chega até ele sem reiniciar. Teste o serviço:

```bash
curl -X POST http://localhost:8000/capture -H "x-api-key: <CAPTURE_API_KEY do .env>" -H "Content-Type: application/json" -d '{"url": "<url-do-dashboard>"}' --output teste.png
```

## 5. Emparelhando o WhatsApp (Evolution API)

O `docker-compose.yml` tem um serviço `evolution-manager` (interface web
oficial pra facilitar esse pareamento), mas ele não sobe: tanto a tag
`latest` quanto `v2.0.0` travam em loop de restart por um bug no container
(ver comentário no `docker-compose.yml`). Não perca tempo com ele -- o
pareamento funciona direto pela API, testado e funcionando em 26/ago/2026:

1. Crie a instância (troque `SUA_CHAVE` pela `EVOLUTION_API_KEY` do `.env`):
   ```bash
   curl -X POST http://localhost:8080/instance/create -H "apikey: SUA_CHAVE" -H "Content-Type: application/json" -d '{"instanceName": "pafil-reporting", "qrcode": true, "integration": "WHATSAPP-BAILEYS"}'
   ```
   A resposta já vem com o QR code em `qrcode.base64` (uma data URI
   `data:image/png;base64,...`).

   O nome `pafil-reporting` não é livre: é o que está na URL do node "Enviar
   WhatsApp" do workflow. Se usar outro, ajuste lá também.
2. Se preferir gerar de novo (o QR expira rápido, uns 20-60s), busque um novo
   com `GET /instance/connect/{instanceName}`:
   ```bash
   curl -s http://localhost:8080/instance/connect/pafil-reporting -H "apikey: SUA_CHAVE" -o qr_response.json
   ```
3. Extraia e salve como imagem (o campo pode vir aninhado diferente entre
   `create` e `connect`, por isso a busca recursiva):
   ```bash
   python3 -c "
   import json, base64

   def find_base64(obj):
       if isinstance(obj, dict):
           if 'base64' in obj and isinstance(obj['base64'], str):
               return obj['base64']
           for v in obj.values():
               r = find_base64(v)
               if r:
                   return r
       return None

   data = json.load(open('qr_response.json'))
   b64 = find_base64(data)
   b64 = b64.split(',', 1)[1] if ',' in b64 else b64
   open('/mnt/c/pafil/qrcode.png', 'wb').write(base64.b64decode(b64))
   "
   ```
   Salvando em `/mnt/c/pafil/` o arquivo já aparece no Windows da VM, pronto
   para abrir.
4. Abra `qrcode.png` e escaneie **imediatamente** com o WhatsApp do número
   que vai enviar os reportings (Configurações → Aparelhos conectados →
   Conectar um aparelho). Demorar pra escanear é a causa mais comum de
   "Não foi possível conectar o dispositivo".
5. Confirme que conectou:
   ```bash
   curl -s http://localhost:8080/instance/connectionState/pafil-reporting -H "apikey: SUA_CHAVE"
   ```
   Deve retornar `"state":"open"`.

## 6. Configurando o envio de email (Outlook, sem SMTP)

Só é necessário se houver destinatário por email. Dá para começar só com
WhatsApp e voltar aqui depois.

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
   ele espera — montada a partir do `WEBHOOK_URL` do `.env`. Copie esse valor e
   cole de volta no campo **URI de redirecionamento** do App Registration
   (passo 3 acima).

   Fazendo tudo pelo navegador da VM, o `WEBHOOK_URL` pode continuar
   `http://localhost:5678/`: o Azure recusa redirect URI em HTTP, mas abre
   exceção justamente para `localhost`. É o caminho mais curto e evita expor a
   porta. Se mudar o `WEBHOOK_URL` depois, a URI cadastrada no Azure precisa
   mudar junto, senão o consentimento volta com `redirect_uri_mismatch`.
3. Clique em **Connect my account** e complete o consentimento (login da
   conta que vai efetivamente enviar os emails).

## 7. Criando o schema `reporting`

É aqui que se configura quem recebe o quê. Roda **no Windows da VM**, onde o
`.env` da raiz do projeto já aponta para o Postgres em `localhost:5432`. A
partir da raiz do `v2/`:

```bash
python aplicar_reporting.py
```

Pode rodar de novo quando quiser: tudo é idempotente. Se o schema já foi
aplicado antes do alerta existir, rode de novo agora (e os `grants`, abaixo)
para criar a coluna `recebe_alertas`, o `alerta_log` e a `v_falhas_do_dia`.

Depois, o role dedicado da automação e suas permissões:

```bash
psql -d pafil_dw -U pafil_app -f automacao_reporting/sql/grants_reporting.sql
```

O script pede a senha do `pafil_n8n` na hora (não passe por linha de comando,
que fica no histórico do shell). Guarde-a junto das outras credenciais do
banco e use a mesma na credencial de Postgres do n8n.

**O que foi criado** (detalhes e comentários em [`sql/reporting.sql`](sql/reporting.sql)):

| Objeto | Para quê |
|---|---|
| `reporting.dashboard` | Uma linha por **página** de dashboard a capturar: título, URL publicada, viewport, espera de renderização |
| `reporting.destinatario` | Nome, WhatsApp (só dígitos, com DDI) e email |
| `reporting.distribuicao` | O cruzamento: quem recebe qual dashboard, por qual canal, em que dias e a partir de que hora |
| `reporting.envio_log` | Auditoria e idempotência: o que já saiu, o que falhou e por quê |
| `reporting.v_fila_envio` | A fila que o n8n consulta — horário, dia da semana, condição e dedupe já resolvidos |
| `reporting.alerta_log` | Um registro por distribuição por dia já avisada, para o alerta não repetir |
| `reporting.v_falhas_do_dia` | O que deveria ter saído hoje, já passou da janela e não saiu, com o último erro |

O n8n não sabe nada de agendamento por destinatário: ele roda de 15 em 15
minutos e pergunta à view o que está elegível **agora**. Toda a regra vive no
banco, então mudar horário ou incluir alguém é um `UPDATE`/`INSERT`, sem tocar
no workflow.

Três decisões que vale conhecer antes de cadastrar:

- **`janela_minutos` (padrão 120)** — depois da `hora`, por quanto tempo ainda
  vale tentar. É o que dá fim ao retry: sem ela, uma falha às 8h ficaria
  repetindo a cada 15 minutos até a meia-noite e a diretoria receberia o print
  às 23h. A janela não pode atravessar a meia-noite (há um CHECK barrando).
- **`condicao_sql`** — opcional; um `SELECT` que devolve uma linha com uma
  coluna booleana, avaliado antes do envio. Ex.:
  `SELECT count(*) > 0 FROM gold.fato_reservas WHERE data_reserva = current_date`.
  Condição quebrada não derruba a fila: vira `WARNING` no log do Postgres e
  aquela linha não sai.
- **Só um envio bem-sucedido por dia** por distribuição, garantido por índice
  parcial. Tentativas com erro podem repetir e ficam todas registradas — é
  esse histórico que responde "por que o print não chegou hoje?".

**Quem recebe o alerta de falha.** A coluna `recebe_alertas` de
`reporting.destinatario` (padrão `false`) marca os administradores. Marque você
mesmo, e quem mais precisar saber quando um envio não sair:

```sql
UPDATE reporting.destinatario SET recebe_alertas = true WHERE destinatario_id = '<seu-id>';
```

Sem ninguém marcado, o workflow de alerta roda e não avisa ninguém, sem erro
nenhum. Quem tem WhatsApp e email cadastrados recebe nos dois, de propósito: se
a causa da falha for o WhatsApp desconectado, o email ainda leva o aviso.

Exemplos de `INSERT` estão comentados no fim do `reporting.sql`. Preencha com
os dados reais direto no banco: URL, telefone e email de gente não entram no
controle de versão (LGPD, mesma regra do `SKILL.md` seção 2).

## 8. Importando os workflows no n8n

No n8n: **Workflows → Import from File**, duas vezes:

- [`workflow_n8n.json`](workflow_n8n.json), o envio em si;
- [`workflow_alerta_n8n.json`](workflow_alerta_n8n.json), o aviso de falha.

Os dois vêm completos e desativados.

**O envio** dispara de 15 em 15 minutos → lê `reporting.v_fila_envio` →
**deduplica por dashboard** (cinco pessoas recebendo o mesmo painel = uma
captura só, e cada captura leva ~15s) → tira os prints em loop → junta cada
print às linhas da fila → separa por canal → envia → grava o resultado em
`reporting.envio_log`. Falha de captura e falha de envio caem no mesmo caminho
de registro, com o motivo.

**O alerta** também roda de 15 em 15 minutos e lê `reporting.v_falhas_do_dia`:
envios que deviam ter saído hoje, já passaram da janela e não saíram. Monta
**uma mensagem só** com todas as falhas do momento (dashboard, destinatário,
canal, horário previsto, número de tentativas e o último erro) e manda por
WhatsApp e por email para quem tem `recebe_alertas`. Só marca a falha como
avisada se pelo menos um canal entregou; se os dois falharem, a rodada seguinte
tenta de novo. Durante a janela de envio não há alerta, porque o envio ainda
está sendo retentado: o aviso só sai depois que a janela fecha.

**Antes de ativar, preencha as credenciais** (os JSONs não as trazem, de
propósito — segredo não se versiona):

| Workflow | Node | Credencial | Valor |
|---|---|---|---|
| envio | `Fila de envio`, `Registrar envio` | Postgres | Host `pafil-db`, porta 5432, base `pafil_dw`, usuário `pafil_n8n`, senha do passo 7 |
| envio | `Capturar print` | Header Auth | Nome `x-api-key`, valor = `CAPTURE_API_KEY` do `.env` |
| envio | `Enviar WhatsApp` | Header Auth | Nome `apikey`, valor = `EVOLUTION_API_KEY` do `.env` |
| envio | `Enviar email` | Microsoft Outlook OAuth2 | A credencial criada na seção 6 |
| alerta | `Falhas do dia`, `Administradores`, `Marcar alertado` | Postgres | A mesma do envio |
| alerta | `Avisar WhatsApp` | Header Auth | A mesma `apikey` do envio |
| alerta | `Avisar email` | Microsoft Outlook OAuth2 | A mesma do envio |

São duas credenciais de Header Auth distintas, com nomes de header
diferentes — não dá para reaproveitar uma no outro node. Cada credencial é
criada uma vez e escolhida em cada node que a usa.

**Sobre o anexo do email.** O node do Outlook espera o anexo em
`Additional Fields → Attachments → Input Data Field Name`, com o valor `data`
(é o nome da propriedade binária que o `Rotular captura` entrega). O workflow já
vem assim. Se o email sair sem anexo, abra o node e confirme que esse campo não
ficou vazio depois da importação.

Rode uma vez com **Execute Workflow** antes de ativar, com uma única
distribuição de teste apontando para o seu próprio número. Só então ligue o
toggle **Active**, nos dois.

## 9. Diagnóstico

A primeira pergunta é sempre "chegou até o banco?". Comece por lá:

```sql
-- O que deveria sair agora
SELECT * FROM reporting.v_fila_envio;
```

```sql
-- O que aconteceu hoje, incluindo as tentativas que falharam
SELECT l.enviado_em, l.status, l.detalhe, d.canal, d.dashboard_id, d.destinatario_id
FROM reporting.envio_log l
JOIN reporting.distribuicao d USING (distribuicao_id)
WHERE l.referencia = current_date
ORDER BY l.enviado_em DESC;
```

| Sintoma | Causa provável |
|---|---|
| `v_fila_envio` vazia quando deveria ter linha | Fora da `janela_minutos`; `dias_semana` errado; `condicao_sql` falsa ou quebrada (procure `WARNING` no log do Postgres); já existe registro `enviado` hoje |
| n8n não conecta no Postgres | Seção 3 incompleta — use os três saltos de verificação para achar qual das três mudanças falta |
| Conectava e parou depois de um reboot | O IP da VM na LAN mudou (DHCP): atualize o `PG_HOST_IP` e recrie os containers, ou reserve o IP no DHCP |
| Captura sempre estoura por timeout | `app.py` na versão antiga, com `networkidle` — ver seção 1 |
| `envio_log` com `401 sessao do Power BI expirada` | A sessão expirou — refaça a seção 4 (gere o `storage_state.json` de novo e rode `docker compose restart capture-service`) |
| `envio_log` com erro de captura por timeout | O dashboard demora mais que `espera_ms` para renderizar, ou o `app.py` está na versão antiga (seção 1) |
| Alerta não chega a ninguém | Ninguém com `recebe_alertas = true`; ou os dois canais falharam (a rodada seguinte tenta de novo; veja as execuções do workflow de alerta no n8n) |
| Alerta chegou, mas `envio_log` está vazio hoje | O envio nunca entrou na fila: confira se o workflow de envio está ativo, se tem execução com erro e se a `condicao_sql` estava falsa |
| Print cortado à direita | Aumente `largura` na `reporting.dashboard` (o painel lateral do Power BI Service come ~350px fixos) |
| WhatsApp não sai | `connectionState` da instância deixou de ser `open` — refaça o pareamento (seção 5) |
| Tudo parou depois de um reboot da VM | O WSL2 não subiu sozinho — ver as duas peças no fim da seção 2 |
| Envio parou do nada, sem erro no n8n | Alguém reexecutou `provisionar_postgres.ps1` e o `pg_hba.conf` voltou ao padrão (ver seção 3) |
| Credenciais do n8n "sumiram" | Volume recriado sem `N8N_ENCRYPTION_KEY` fixa no `.env` (ver seção 1) |

## 10. O que foi testado e o que falta

**Testado em 30/set/2026, num n8n 2.41 local** (notebook, fora da VM), com
Postgres descartável, o `capture_service` real (Playwright) e uma Evolution API
de mentira: o schema `reporting` aplicado duas vezes (idempotência) e com os
grants do `pafil_n8n`; a fila (horário, dia da semana, condição boa, ruim e
quebrada, contato ausente); o envio ponta a ponta com dois dashboards e três
destinatários; a deduplicação de captura (um print para várias linhas); as duas
falhas (sessão expirada na captura e número recusado no envio) caindo no
`envio_log` com o motivo legível; a reexecução sem reenviar o que já saiu; e o
alerta com a mensagem consolidada e o dedupe do `alerta_log`. Esse teste achou e
corrigiu: o anexo do email no formato do Gmail (o Outlook ignorava e mandaria
email sem anexo), o nome do arquivo anexado ("capture", sem extensão), o erro do
Outlook que o registro perdia, e a tela de login do Power BI que seguiria como
se fosse o dashboard.

**Não coberto pelo teste, e só se confirma na VM:**

- O envio de email de verdade. O node do Outlook foi exercitado até a porta da
  credencial (o anexo é lido e a falha vem só de "sem credenciais"), mas sem
  conta conectada não há como ver o email chegar.
- O WhatsApp de verdade (a instância `pafil-reporting` pareada), a captura do
  Power BI autenticado e o Postgres de produção pela rede da VM (seção 3).
- O motor de expressões do n8n da VM é o `vm` (padrão da imagem atual); o teste
  local usou o `legacy`. As expressões dos workflows são JavaScript comum, mas
  vale olhar a primeira execução manual com atenção.

**Limites conhecidos, por decisão:**

- O alerta roda dentro do mesmo n8n que envia. Se o n8n, o WSL2 ou a VM estiverem
  fora do ar, nenhum dos dois workflows roda e ninguém é avisado. Cobrir isso
  exige algo fora da VM (um monitor externo batendo no n8n), que hoje não existe.
- O alerta cobre a falha de envio, não o dashboard errado. Se o Power BI entregar
  um painel vazio ou com dados velhos, o print sai normalmente.
- A sessão do Power BI expira e precisa ser renovada à mão (seção 4). Agora o
  sintoma é um alerta (`401 sessao do Power BI expirada`), e não mais um print de
  tela de login chegando à diretoria.
