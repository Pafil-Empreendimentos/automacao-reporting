# Automação de reporting

Envia, sem ninguém apertar botão, o print de dashboards publicados no Power BI Service para
uma lista de pessoas, por WhatsApp e por e-mail. Quem recebe o quê, em que dia e em que
horário fica cadastrado no banco, e o n8n só executa.

Tudo roda em software livre e hospedado pela própria empresa, sem assinatura nova.

## Como funciona

```
 a cada 15 min
      │
      ▼
   n8n ──► reporting.v_fila_envio (Postgres)      o que deve sair agora?
      │
      ├──► capture_service (Playwright)           tira o print do dashboard
      │
      ├──► Evolution API ──► WhatsApp
      └──► Microsoft Outlook (Graph API) ──► e-mail
      │
      ▼
   reporting.envio_log                            o que saiu, o que falhou e por quê
```

- Um segundo workflow, `alerta`, roda no mesmo ritmo e avisa os administradores quando um
  envio do dia não saiu dentro da janela prevista.
- O n8n não conhece horários nem destinatários. Ele pergunta à view o que está elegível naquele
  momento. Mudar um horário ou incluir alguém é um `UPDATE` ou `INSERT` no banco, sem tocar
  no workflow.

## O que tem aqui

```
.
├── docker-compose.yml        # n8n, capture_service, Evolution API e o banco dela
├── capture_service/          # serviço que tira o print (Playwright)
├── workflows/
│   ├── envio_n8n.json        # fluxo de envio, para importar no n8n
│   └── alerta_n8n.json       # aviso de envio que não saiu
├── sql/
│   ├── reporting.sql         # schema reporting (destinatários, distribuição, log, filas)
│   └── grants_reporting.sql  # role pafil_n8n, com as permissões mínimas
├── scripts/
│   └── aplicar_reporting.py  # aplica o schema e confere os objetos (alternativa ao psql)
└── docs/
    └── guia-completo.md      # instalação, operação e diagnóstico, passo a passo
```

## Onde roda

Na mesma VM Windows que hospeda o PostgreSQL de produção do
[data-hub-pafil](https://github.com/arturvillasboas/data-hub-pafil). A stack roda em Docker
Engine dentro do WSL2 (e não no Docker Desktop, para não adicionar licença).

Um ponto que costuma confundir: o Postgres está na mesma VM, mas do lado Windows, enquanto os
containers estão dentro do WSL2. São duas redes diferentes, então `localhost` dentro do container
não é o `localhost` do Windows. O n8n alcança o banco pelo IP da VM na rede da empresa, que o
compose expõe como o apelido `pafil-db`. O guia explica isso na seção 3.

## Começando

1. Clone o repositório em `C:\pafil\automacao-reporting` (no WSL, `/mnt/c/pafil/automacao-reporting`).
2. Copie `.env.example` para `.env` e preencha as chaves.
3. Siga o [guia completo](docs/guia-completo.md). A seção 1 traz a ordem de go-live, de
   ponta a ponta, com a referência de cada passo.

Se a stack já está de pé na VM, vinda de quando esta pasta morava no `data-hub-pafil`, não
clone antes de ler a seção 11 do guia: ela preserva os volumes do Docker, que guardam as
credenciais do n8n e o pareamento do WhatsApp.

## Estado atual

O código está pronto e foi testado ponta a ponta num n8n local (com a Evolution API simulada).
Os containers sobem na VM, mas o envio de verdade ainda não foi ativado. Falta:

- gerar a sessão autenticada do Power BI e parear o WhatsApp (seções 4 e 5 do guia);
- configurar o e-mail pelo Outlook, que pode ficar para depois: o WhatsApp funciona sem ele;
- cadastrar um dashboard e um destinatário de teste, rodar uma vez à mão e só então ativar os
  dois workflows.

O guia, na seção 10, lista o que o teste local cobriu e o que só dá para confirmar na VM.

## Cuidados

- O `storage_state.json` (sessão do Power BI) e o `.env` nunca vão para o Git.
- Telefone e e-mail de destinatários ficam só no banco, nunca no repositório.
- Reexecutar o `provisionar_postgres.ps1` do data-hub-pafil reescreve o `pg_hba.conf` e derruba
  o acesso do n8n em silêncio. Veja a seção 3 do guia antes de reexecutá-lo.
