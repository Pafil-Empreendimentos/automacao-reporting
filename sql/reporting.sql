-- Schema `reporting` — configuração de distribuição dos dashboards por
-- WhatsApp/email. É metadado operacional da automação, NÃO faz parte da
-- linhagem bronze -> silver -> gold; por isso mora aqui, junto da stack que o
-- consome, e não em sql/<camada>/. Mora no mesmo banco de produção (pafil_dw),
-- que a VM da automação alcança pela rede interna — ver README.md seção 3, que
-- cobre a liberação necessária (listen_addresses, pg_hba.conf e firewall).
--
--   python aplicar_reporting.py            (a partir da raiz do v2)
--
-- Idempotente: CREATE ... IF NOT EXISTS / CREATE OR REPLACE em tudo, pode
-- rodar de novo a cada mudança.

CREATE SCHEMA IF NOT EXISTS reporting;

-- --- O que capturar ----------------------------------------------------------
-- Uma linha por PÁGINA de dashboard, não por relatório: o print é da URL que
-- estiver aqui, então um relatório com três páginas que precisam ir por
-- WhatsApp vira três linhas, cada uma com a URL da sua página (o Power BI
-- Service coloca a página na própria URL, em .../ReportSection<n>).
CREATE TABLE IF NOT EXISTS reporting.dashboard (
    dashboard_id text PRIMARY KEY,          -- slug, ex.: 'vendas-diario'
    titulo       text NOT NULL,             -- vira o assunto do email / legenda
    url          text NOT NULL,             -- link publicado, já na página certa
    -- Mesmos defaults do capture_service/app.py: 2400x1350 porque o painel de
    -- navegação lateral do Power BI Service come ~350px fixos da esquerda.
    largura      int  NOT NULL DEFAULT 2400 CHECK (largura BETWEEN 800 AND 4000),
    altura       int  NOT NULL DEFAULT 1350 CHECK (altura  BETWEEN 600 AND 3000),
    -- Espera depois do "load" para os visuais terminarem de renderizar (as
    -- chamadas de DAX continuam depois do carregamento inicial da página).
    espera_ms    int  NOT NULL DEFAULT 10000 CHECK (espera_ms BETWEEN 0 AND 120000),
    ativo        boolean NOT NULL DEFAULT true
);

-- --- Para quem ---------------------------------------------------------------
-- Contém dado pessoal (telefone/email de funcionário): o schema não é exposto
-- ao role de leitura do Power BI (pafil_bi), ver grants_reporting.sql.
CREATE TABLE IF NOT EXISTS reporting.destinatario (
    destinatario_id text PRIMARY KEY,       -- slug, ex.: 'joao-diretoria'
    nome            text NOT NULL,
    -- Formato exigido pelo Evolution API: DDI + DDD + número, só dígitos, sem
    -- '+', sem espaço e sem traço. Ex.: 5511999998888.
    whatsapp        text CHECK (whatsapp ~ '^[0-9]{12,15}$'),
    email           text CHECK (email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'),
    ativo           boolean NOT NULL DEFAULT true,
    CHECK (whatsapp IS NOT NULL OR email IS NOT NULL)
);

-- Quem é avisado quando um envio falha (ver v_falhas_do_dia). Fica fora do
-- CREATE TABLE de propósito: o ADD COLUMN IF NOT EXISTS também vale onde o
-- schema já foi aplicado antes desta coluna existir.
ALTER TABLE reporting.destinatario
    ADD COLUMN IF NOT EXISTS recebe_alertas boolean NOT NULL DEFAULT false;

-- --- Quem recebe o quê, por onde, quando -------------------------------------
CREATE TABLE IF NOT EXISTS reporting.distribuicao (
    distribuicao_id bigserial PRIMARY KEY,
    dashboard_id    text NOT NULL REFERENCES reporting.dashboard (dashboard_id)
                        ON UPDATE CASCADE ON DELETE CASCADE,
    destinatario_id text NOT NULL REFERENCES reporting.destinatario (destinatario_id)
                        ON UPDATE CASCADE ON DELETE CASCADE,
    canal           text NOT NULL CHECK (canal IN ('whatsapp', 'email')),
    -- Dias da semana em ISO (1=segunda ... 7=domingo). Default = dias úteis.
    dias_semana     int[] NOT NULL DEFAULT '{1,2,3,4,5}',
    -- Hora local (America/Sao_Paulo) a partir da qual o envio fica elegível.
    -- Não é um horário exato: o gatilho do n8n roda de 15 em 15 minutos, então
    -- o envio sai na primeira rodada depois desta hora.
    hora            time NOT NULL,
    -- Janela, em minutos depois de `hora`, em que ainda vale tentar. Existe
    -- para o retry ter fim: sem ela, uma falha às 8h ficaria repetindo a cada
    -- 15min até meia-noite e o destinatário receberia o print fora de hora.
    janela_minutos  int NOT NULL DEFAULT 120 CHECK (janela_minutos BETWEEN 15 AND 1440),
    -- Condição opcional. Precisa ser um SELECT que devolve UMA linha com UMA
    -- coluna booleana; NULL/vazio = envia sempre. Ex.:
    --   SELECT count(*) > 0 FROM gold.fato_reservas WHERE data_reserva = current_date
    -- Avaliado por reporting.avaliar_condicao (ver abaixo) com os privilégios
    -- de quem consulta a fila — o role do n8n só enxerga a gold, e é essa
    -- limitação, não o texto da condição, que contém o estrago possível aqui.
    condicao_sql    text,
    -- Texto que acompanha o print (legenda no WhatsApp, corpo no email).
    -- NULL = usa o título do dashboard.
    legenda         text,
    ativo           boolean NOT NULL DEFAULT true,
    UNIQUE (dashboard_id, destinatario_id, canal, hora),
    -- A janela não pode atravessar a meia-noite. `time` dá a volta no relógio
    -- (23:00 + 2h = 01:00), então uma janela que vira o dia ficaria com fim
    -- ANTES do início e a linha nunca sairia da fila — falha silenciosa. Se um
    -- dia precisar de envio de madrugada, cadastre a hora já do lado de lá da
    -- meia-noite em vez de esticar a janela.
    CHECK (hora + make_interval(mins => janela_minutos) > hora)
);

CREATE INDEX IF NOT EXISTS ix_distribuicao_ativo
    ON reporting.distribuicao (ativo, hora);

-- --- Auditoria e idempotência ------------------------------------------------
CREATE TABLE IF NOT EXISTS reporting.envio_log (
    envio_id        bigserial PRIMARY KEY,
    distribuicao_id bigint NOT NULL REFERENCES reporting.distribuicao (distribuicao_id)
                        ON DELETE CASCADE,
    referencia      date NOT NULL,          -- dia de competência do envio
    enviado_em      timestamptz NOT NULL DEFAULT now(),
    status          text NOT NULL CHECK (status IN ('enviado', 'erro')),
    detalhe         text
);

-- Índice parcial (e não UNIQUE na tabela) de propósito: garante um único envio
-- BEM-SUCEDIDO por distribuição por dia, mas deixa vários registros de 'erro'
-- conviverem — é o histórico de tentativas que explica o que aconteceu quando
-- alguém pergunta por que o print não chegou.
CREATE UNIQUE INDEX IF NOT EXISTS ux_envio_log_enviado
    ON reporting.envio_log (distribuicao_id, referencia)
    WHERE status = 'enviado';

CREATE INDEX IF NOT EXISTS ix_envio_log_referencia
    ON reporting.envio_log (referencia DESC);

-- --- Avaliação da condição ---------------------------------------------------
-- Existe para a fila ser resolvida numa consulta só. A alternativa era o n8n
-- ler a fila, rodar cada condicao_sql num node separado e reconciliar os
-- resultados com as linhas de origem — muito mais nó e mais chance de erro do
-- que um EXECUTE aqui dentro.
--
-- SEM security definer, de propósito: roda com os privilégios de quem chama,
-- então o role do n8n não alcança pela condição nada que já não alcançasse
-- direto (ver grants_reporting.sql — ele só tem SELECT na gold).
CREATE OR REPLACE FUNCTION reporting.avaliar_condicao(p_sql text)
RETURNS boolean
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_resultado boolean;
BEGIN
    IF p_sql IS NULL OR btrim(p_sql) = '' THEN
        RETURN true;
    END IF;

    EXECUTE p_sql INTO v_resultado;
    RETURN coalesce(v_resultado, false);
EXCEPTION
    WHEN OTHERS THEN
        -- Uma condição quebrada (coluna renomeada na gold, erro de digitação)
        -- não pode derrubar a fila inteira e travar os outros destinatários.
        -- Vira aviso no log do Postgres e a linha simplesmente não sai.
        RAISE WARNING 'reporting: condicao_sql falhou (%): %', p_sql, SQLERRM;
        RETURN false;
END;
$$;

COMMENT ON FUNCTION reporting.avaliar_condicao(text) IS
    'Avalia a condicao_sql de uma distribuição. NULL/vazio => true. Erro => false + WARNING.';

-- --- A fila que o n8n consulta -----------------------------------------------
-- Uma linha por envio a fazer AGORA. O n8n não precisa saber de horário, de
-- dia da semana nem do que já foi enviado: tudo isso está resolvido aqui.
CREATE OR REPLACE VIEW reporting.v_fila_envio AS
WITH agora AS (
    SELECT (now() AT TIME ZONE 'America/Sao_Paulo') AS local_ts
)
SELECT
    d.distribuicao_id,
    agora.local_ts::date                          AS referencia,
    d.canal,
    dash.dashboard_id,
    dash.titulo,
    dash.url,
    dash.largura,
    dash.altura,
    dash.espera_ms,
    dest.nome                                     AS destinatario_nome,
    CASE d.canal
        WHEN 'whatsapp' THEN dest.whatsapp
        WHEN 'email'    THEN dest.email
    END                                           AS destino,
    coalesce(d.legenda, dash.titulo)              AS legenda
FROM reporting.distribuicao d
JOIN reporting.dashboard    dash USING (dashboard_id)
JOIN reporting.destinatario dest USING (destinatario_id)
CROSS JOIN agora
WHERE d.ativo
  AND dash.ativo
  AND dest.ativo
  -- Destinatário sem o contato do canal escolhido some da fila em vez de
  -- virar erro de envio a cada 15 minutos.
  AND CASE d.canal
          WHEN 'whatsapp' THEN dest.whatsapp
          WHEN 'email'    THEN dest.email
      END IS NOT NULL
  AND extract(isodow FROM agora.local_ts)::int = ANY (d.dias_semana)
  AND agora.local_ts::time >= d.hora
  AND agora.local_ts::time <  d.hora + make_interval(mins => d.janela_minutos)
  AND NOT EXISTS (
        SELECT 1
        FROM reporting.envio_log l
        WHERE l.distribuicao_id = d.distribuicao_id
          AND l.referencia      = agora.local_ts::date
          AND l.status          = 'enviado'
      )
  AND reporting.avaliar_condicao(d.condicao_sql);

COMMENT ON VIEW reporting.v_fila_envio IS
    'Envios elegíveis neste instante (horário, dia da semana, condição e dedupe já aplicados).';

-- --- Alerta de envio que não saiu --------------------------------------------
-- O envio_log guarda o motivo de cada falha, mas ninguém olha uma tabela. Esta
-- view responde à pergunta que importa: "o que deveria ter saído hoje, já
-- passou da janela e não saiu?". Um segundo workflow do n8n (workflow_alerta_
-- n8n.json) a consulta e avisa quem tem recebe_alertas = true.
--
-- O aviso sai depois que a janela fecha, e não a cada falha: durante a janela o
-- envio ainda é retentado de 15 em 15 minutos, então uma falha isolada das 8h
-- que se resolve às 8h15 não vira alarme. O alerta_log garante um aviso por
-- distribuição por dia.
CREATE TABLE IF NOT EXISTS reporting.alerta_log (
    distribuicao_id bigint NOT NULL REFERENCES reporting.distribuicao (distribuicao_id)
                        ON DELETE CASCADE,
    referencia      date NOT NULL,
    alertado_em     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (distribuicao_id, referencia)
);

CREATE OR REPLACE VIEW reporting.v_falhas_do_dia AS
WITH agora AS (
    SELECT (now() AT TIME ZONE 'America/Sao_Paulo') AS local_ts
)
SELECT
    d.distribuicao_id,
    agora.local_ts::date                          AS referencia,
    d.canal,
    dash.dashboard_id,
    dash.titulo,
    dest.nome                                     AS destinatario_nome,
    d.hora,
    (SELECT count(*)
       FROM reporting.envio_log l
      WHERE l.distribuicao_id = d.distribuicao_id
        AND l.referencia      = agora.local_ts::date)  AS tentativas,
    (SELECT l.detalhe
       FROM reporting.envio_log l
      WHERE l.distribuicao_id = d.distribuicao_id
        AND l.referencia      = agora.local_ts::date
      ORDER BY l.enviado_em DESC
      LIMIT 1)                                    AS ultimo_erro
FROM reporting.distribuicao d
JOIN reporting.dashboard    dash USING (dashboard_id)
JOIN reporting.destinatario dest USING (destinatario_id)
CROSS JOIN agora
WHERE d.ativo
  AND dash.ativo
  AND dest.ativo
  AND CASE d.canal
          WHEN 'whatsapp' THEN dest.whatsapp
          WHEN 'email'    THEN dest.email
      END IS NOT NULL
  AND extract(isodow FROM agora.local_ts)::int = ANY (d.dias_semana)
  -- Janela já fechada (o CHECK de distribuicao garante que ela não vira o dia).
  AND agora.local_ts::time >= d.hora + make_interval(mins => d.janela_minutos)
  AND NOT EXISTS (
        SELECT 1
        FROM reporting.envio_log l
        WHERE l.distribuicao_id = d.distribuicao_id
          AND l.referencia      = agora.local_ts::date
          AND l.status          = 'enviado'
      )
  AND NOT EXISTS (
        SELECT 1
        FROM reporting.alerta_log a
        WHERE a.distribuicao_id = d.distribuicao_id
          AND a.referencia      = agora.local_ts::date
      )
  -- Condição falsa = "hoje não era para enviar" (ex.: sem venda no dia), não falha.
  AND reporting.avaliar_condicao(d.condicao_sql);

COMMENT ON VIEW reporting.v_falhas_do_dia IS
    'Envios que deviam ter saído hoje, já passaram da janela, não saíram e ainda não geraram alerta.';

-- --- Exemplos de configuração ------------------------------------------------
-- Comentados de propósito: URL, telefone e email reais entram à mão, direto no
-- banco de produção, e não versionados aqui (LGPD — ver SKILL.md seção 2).
--
-- INSERT INTO reporting.dashboard (dashboard_id, titulo, url) VALUES
--   ('vendas-diario', 'Vendas — acompanhamento diário',
--    'https://app.powerbi.com/groups/<grupo>/reports/<id>/<ReportSection>');
--
-- INSERT INTO reporting.destinatario (destinatario_id, nome, whatsapp, email) VALUES
--   ('diretoria', 'Diretoria', '5511999998888', 'diretoria@exemplo.com.br');
--
-- -- Quem é avisado quando um envio não sai (ver v_falhas_do_dia):
-- UPDATE reporting.destinatario SET recebe_alertas = true WHERE destinatario_id = 'diretoria';
--
-- INSERT INTO reporting.distribuicao
--   (dashboard_id, destinatario_id, canal, hora, legenda) VALUES
--   ('vendas-diario', 'diretoria', 'whatsapp', '08:30',
--    'Vendas de ontem — atualizado hoje de manhã');
