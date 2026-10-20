-- grants_reporting.sql — role dedicado para a automação de reporting.
--
-- Rodar DEPOIS de aplicar_reporting.py, e a partir da raiz do v2:
--
--   psql -d pafil_dw -U pafil_app -f automacao_reporting/sql/grants_reporting.sql
--
-- A senha é pedida na hora (\prompt), para não ficar no histórico do shell nem
-- na lista de processos. Guarde-a junto das outras, no mesmo arquivo de
-- credenciais que o provisionar_postgres.ps1 escreve, e use o mesmo valor no
-- .env da stack (PG_N8N_PASSWORD). Em execução não interativa (CI, script),
-- passe `-v senha_n8n=<senha>` — sem aspas — que o \prompt não dispara.
--
-- Por que um role novo, e não o pafil_app nem o pafil_bi:
--   - pafil_app é o dono do banco (DDL em bronze/silver/gold). Dar essa conta a
--     um container que fica exposto numa porta HTTP é o oposto do que a
--     separação de roles do provisionar_postgres.ps1 quis fazer.
--   - pafil_bi é somente leitura, e a automação precisa gravar o envio_log.
--
-- O que este role alcança, e só isso: a fila (view), o log de envio (INSERT) e
-- a gold. A gold entra porque é onde as condicao_sql fazem sentido ("teve
-- venda hoje?"). Bronze e silver ficam de fora de propósito: é onde mora a PII
-- em estado bruto, e nenhuma condição de disparo precisa dela.

\set ON_ERROR_STOP on

\if :{?senha_n8n}
\else
\prompt 'Senha para o role pafil_n8n: ' senha_n8n
\endif

-- \gexec em vez de um bloco DO: o psql NÃO interpola variáveis dentro de
-- string com dollar-quoting, então `:senha_n8n` chegaria literal ao servidor e
-- o DO $$...$$ falharia. Aqui a variável é interpolada como literal escapado
-- (`:'senha_n8n'`) na consulta que MONTA o comando, e o \gexec executa o
-- texto resultante.
SELECT format('CREATE ROLE pafil_n8n LOGIN PASSWORD %L', :'senha_n8n')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pafil_n8n')
\gexec

-- ALTER incondicional logo depois do CREATE condicional, mesmo motivo
-- documentado no provisionar_postgres.ps1: numa máquina reaproveitada o role
-- pode já existir com senha diferente da que está no .env, e só um ALTER sem
-- condição garante que banco e arquivo de credenciais contem a mesma história.
SELECT format('ALTER ROLE pafil_n8n WITH LOGIN PASSWORD %L', :'senha_n8n')
\gexec

GRANT CONNECT ON DATABASE pafil_dw TO pafil_n8n;

-- --- A automação em si -------------------------------------------------------
GRANT USAGE ON SCHEMA reporting TO pafil_n8n;

GRANT SELECT ON reporting.v_fila_envio  TO pafil_n8n;
GRANT SELECT ON reporting.dashboard     TO pafil_n8n;
GRANT SELECT ON reporting.destinatario  TO pafil_n8n;
GRANT SELECT ON reporting.distribuicao  TO pafil_n8n;

-- Escrita só no log, e só inserindo: o n8n registra o que fez, nunca reescreve
-- histórico. Sem UPDATE/DELETE, um workflow com defeito não consegue apagar o
-- rastro do que já enviou.
GRANT INSERT, SELECT ON reporting.envio_log TO pafil_n8n;
GRANT USAGE ON SEQUENCE reporting.envio_log_envio_id_seq TO pafil_n8n;

-- Alerta de envio que não saiu (workflow_alerta_n8n.json): lê a view e marca o
-- que já avisou. Mesmo critério do envio_log, só insere.
GRANT SELECT ON reporting.v_falhas_do_dia TO pafil_n8n;
GRANT INSERT, SELECT ON reporting.alerta_log TO pafil_n8n;

-- --- Base para as condições de disparo ---------------------------------------
GRANT USAGE  ON SCHEMA gold                TO pafil_n8n;
GRANT SELECT ON ALL TABLES IN SCHEMA gold  TO pafil_n8n;

ALTER DEFAULT PRIVILEGES FOR ROLE pafil_app IN SCHEMA gold
  GRANT SELECT ON TABLES TO pafil_n8n;

-- --- Barreiras explícitas ----------------------------------------------------
REVOKE ALL ON SCHEMA bronze FROM pafil_n8n;
REVOKE ALL ON SCHEMA silver FROM pafil_n8n;

-- --- Conferência -------------------------------------------------------------
-- Deve listar gold e reporting, e NENHUMA linha de bronze ou silver.
SELECT table_schema, count(*) AS objetos_legiveis
FROM information_schema.table_privileges
WHERE grantee = 'pafil_n8n' AND privilege_type = 'SELECT'
GROUP BY table_schema
ORDER BY table_schema;
