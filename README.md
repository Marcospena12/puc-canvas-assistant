# Assistente Canvas PUC Minas

Assistente acadêmico pessoal que lê os dados do Canvas LMS (somente leitura),
guarda um cache local e responde perguntas em linguagem natural pelo Telegram
ou pelo terminal.

## O que já funciona (Fases 1, 2 e 3.1)

- Sincronização de cursos, tarefas, prazos do planner, avisos, arquivos e módulos.
- Cópia local incremental dos arquivos das matérias ativas em `data/files/<course_id>/<file_id>_<nome_sanitizado>` com hash SHA-256 no SQLite.
- Downloader seguro que redireciona links pré-assinados sem repassar o header `Authorization` para terceiros (como S3/CloudFront).
- Filtro configurável de tipos aceitos (padrão: PDF, PPTX, DOCX, TXT/MD) e limite de tamanho (padrão: 50 MB), marcando itens fora do escopo como ignorados com o motivo.
- Tratamento resiliente de matérias com aba Arquivos bloqueada (401/403): registra status, aproveita arquivos em módulos quando disponíveis e retém backoff configurável (padrão 24h) sem insistir a cada ciclo.
- Notificações proativas estendidas: aviso de arquivo novo por matéria (`NOTIFY_NEW_FILES=true`), com baseline preservado em reinicializações e upgrades.
- Bot do Telegram restrito ao seu usuário/chat, com comandos `/sincronizar` (ou `/sync`), `/arquivos <matéria>`, `/buscar <termo>`, `/trecho <n>`, `/explicar`, `/resumo`, `/quiz`, `/gabarito`, `/uso`, `/status`, `/testnotify` e `/help`.
- Base de conhecimento FTS5 com extratores para PDF, PPTX, DOCX e TXT, chunking com teto inviolável de slides e sinalização de cobertura forte/parcial.
- Comandos de estudo com LLM fundamentados no material de aula (`/explicar`, `/resumo` com cache SHA-256 e `/quiz` com validação de esquema JSON).
- Cache SQLite local com atualização automática (Canvas a cada 20 min; download de arquivos a cada 1 hora).
- Agente LLM com ferramentas para consultar o cache, contexto de hoje/amanhã, busca em materiais de aula e validação de links contra os dados retornados (anti-alucinação).
- Respostas em HTML seguro, com negrito, itálico, código e links clicáveis.
- CLI para validar configuração, sincronizar dados, baixar arquivos, listar arquivos conhecidos, perguntar, buscar materiais, gerar resumos/quizzes e ver status/uso.
- LLM local via Ollama (`qwen3:14b`), APIs compatíveis com OpenAI (OpenAI, Grok, Gemini) ou Anthropic (Claude).

## Requisitos

- Windows com PowerShell (funciona em Linux/macOS também).
- [uv](https://docs.astral.sh/uv/) e Python 3.12 (o `uv` baixa o Python se necessário).
- [Ollama](https://ollama.com/) com o modelo padrão:

```powershell
ollama pull qwen3:14b
ollama serve
```

Alternativa: usar um provider na nuvem configurando `LLM_PROVIDER` e `LLM_API_KEY`.

## Configuração

```powershell
uv sync
Copy-Item .env.example .env
```

Edite o `.env`:

- `CANVAS_BASE_URL`: endereço base do Canvas, sem `/api/v1`.
- `CANVAS_TOKEN`: token em Canvas > Conta > Configurações > Novo token de acesso.
- `TELEGRAM_BOT_TOKEN`: token criado no `@BotFather`.
- `TELEGRAM_CHAT_ID`: seu id de chat (use o `@userinfobot`).
- `TELEGRAM_ALLOWED_USER_IDS`: ids de usuários autorizados, separados por vírgula.

O bot se recusa a iniciar sem `TELEGRAM_CHAT_ID` e/ou `TELEGRAM_ALLOWED_USER_IDS`.
Nunca comite o `.env`: ele já está no `.gitignore`.

Notificações (opcionais, com os padrões abaixo):

- `NOTIFY_ENABLED`: liga/desliga o serviço (`true`).
- `NOTIFY_NEW_ASSIGNMENTS`: avisa sobre tarefas novas (`true`).
- `NOTIFY_DUE_HOURS`: lembretes antes do prazo, em horas (`24,2`).
- `NOTIFY_ANNOUNCEMENTS`: avisa sobre avisos novos (`true`).
- `NOTIFY_DIGEST` e `NOTIFY_DIGEST_TIME`: resumo diário e horário (`true`, `08:00`).
- `NOTIFY_QUIET_START`/`NOTIFY_QUIET_END`: janela de silêncio (`22:00`, `07:00`);
  use o mesmo horário nos dois campos para desativar.
- `NOTIFY_MAX_STALE_MINUTES`: não notifica se a última sincronização for mais
  antiga que isso (`45`).
- `NOTIFY_CHECK_MINUTES`: intervalo de verificação (`5`).
- `NOTIFY_DIGEST_GRACE_MINUTES`: janela para enviar o resumo após o horário (`120`).

Na primeira execução, ou ao reativar as notificações, o estado atual vira baseline:
itens já existentes não geram mensagens. O envio só ocorre após uma sincronização
bem-sucedida e recente.

## Uso

```powershell
# valida Canvas, banco e modelo local
uv run python -m canvas_assistant check

# baixa os dados do Canvas para o cache local
uv run python -m canvas_assistant sync

# sincroniza e baixa incrementalmente os arquivos das matérias ativas
uv run python -m canvas_assistant sync-files

# lista os arquivos conhecidos (todas ou de uma matéria)
uv run python -m canvas_assistant files
uv run python -m canvas_assistant files "Bancos"

# pergunta pelo terminal, sem Telegram
uv run python -m canvas_assistant ask "o que tenho para esta semana?"

# pesquisa nos materiais das aulas (FTS5 com sinal de cobertura)
uv run python -m canvas_assistant search "modelagem dimensional" --course "BI"

# comandos de estudo via CLI
uv run python -m canvas_assistant study explain "modelagem dimensional" --course "BI"
uv run python -m canvas_assistant study summary "BI" "Aula02"
uv run python -m canvas_assistant study quiz "BI" "modelagem dimensional" -n 5
uv run python -m canvas_assistant study usage

# inicia o bot (sincroniza e depois faz refresh a cada 20 min)
uv run python -m canvas_assistant run

# estado do cache e falhas recentes
uv run python -m canvas_assistant status

# backup consistente do banco de dados (retenção padrão: 7 cópias)
uv run python -m canvas_assistant backup
uv run python -m canvas_assistant backup --keep 14

# verifica a integridade do backup mais recente (ou de um arquivo específico)
uv run python -m canvas_assistant backup-check
```

No Telegram:

- `o que tenho pra hoje?`
- `como estão minhas notas?`
- `quais matérias estou cursando?`
- `tem aviso novo?`
- `quais arquivos tem em Cálculo?`
- `/tarefas [matéria]` - lista tarefas pendentes ordenadas (atrasadas e hoje no topo)
- `/tarefa <n | título>` - briefing estruturado com rubrica e materiais
- `/buscar [matéria:] <termo>` - busca em materiais com sinal de cobertura
- `/trecho <n>` - exibe o texto completo de um resultado
- `/explicar [matéria:] <tópico>` - explicação fundamentada com fontes
- `/resumo <matéria> <arquivo> [faixa]` - resumo estruturado com cache
- `/quiz <matéria> <tópico> [n]` - quiz formativo baseado nas aulas
- `/gabarito` - respostas comentadas do último quiz
- `/uso` - métricas de tokens dos últimos 7 dias
- `/status` - estado do cache e falhas recentes
- `/saude` - diagnóstico do banco, integridade, últimos syncs, LLM e incidentes
- `/sincronizar` (ou `/sync`), `/arquivos <matéria>`, `/testnotify`, `/help`

O agente só responde a perguntas de tutoria. Ele não resolve quizzes nem
entrega tarefas: integridade acadêmica é uma regra do prompt e do escopo do projeto.

## Arquitetura

```text
src/canvas_assistant/
├── config.py          # Settings, perfil, LLM, notificações e sincronização de arquivos
├── db.py              # schema SQLite (v3, inclui file_sync_state e file_course_sync)
├── repository.py      # upserts, consultas, metadados e catálogo de arquivos
├── notifications.py   # baseline (incluindo arquivos), dedupe, lembretes, avisos e resumo
├── canvas/
│   ├── client.py      # HTTP read-only, paginação, retry, rate limit
│   ├── service.py     # endpoints de domínio do Canvas (cursos, arquivos, pastas, módulos)
│   └── errors.py      # erros tipados (401, 403, 404, 429, 5xx)
├── sync.py            # orquestra a sincronização, isola falhas e respeita backoff de arquivos
├── knowledge/         # Base de conhecimento (Fase 3)
│   ├── downloader.py  # download seguro com redirect manual e remoção de auth em terceiros
│   ├── file_sync.py   # catálogo unificado, verificação incremental, sanitização e download
│   └── report.py      # resolução de matérias sem acento e relatórios de /arquivos
├── textutil.py        # nomes curtos de matéria
├── urls.py            # normalização/extração de URLs e ids de recursos
├── agent/
│   ├── agent.py       # loop de tool calling, histórico e correção de grounding
│   ├── context.py     # snapshot de hoje/amanhã e avisos recentes
│   ├── grounding.py   # evidência do turno, validação e fallback determinístico
│   ├── prompts.py     # prompt de sistema em pt-BR
│   └── tools.py       # 6 ferramentas + validação com pydantic
├── llm/               # Ollama nativo e APIs compatíveis com OpenAI
├── channels/          # interface de canal, adaptador do Telegram e formatação HTML
├── app.py             # bot, schedulers (refresh 20m, arquivos 1h) e fiação geral
└── cli.py             # run, check, sync, sync-files, files, ask, status
```

Fluxo resumido:

1. `sync` consulta as abas do Canvas, pagina os endpoints e grava tudo no SQLite.
2. A cada 20 minutos o scheduler executa a mesma sincronização (sem sobreposição).
3. Após um sync bem-sucedido, o serviço de notificações verifica tarefas/avisos
   novos, lembretes de prazo e o resumo diário, respeitando silêncio e dedupe.
4. Uma mensagem do Telegram vira uma pergunta ao LLM com as 6 ferramentas disponíveis,
   um snapshot de prazos de hoje/amanhã e avisos recentes.
5. O LLM escolhe ferramentas; elas leem apenas o cache local e devolvem JSON.
6. A resposta é validada: links/ids que não vieram dos dados disparam uma correção.
   Se a correção falhar, o bot usa um resumo determinístico dos dados encontrados.
7. A resposta final é convertida para HTML seguro e enviada em partes de até 3000 caracteres.

O Canvas é acessado somente com `GET`. Nenhuma submissão, alteração ou exclusão é feita.

## Dados e privacidade

- `.env`: segredos, ignorado pelo Git.
- `data/assistant.sqlite3`: cache local, ignorado pelo Git.
- `canvas_dump/`: dumps de exploração, ignorados pelo Git (podem conter dados pessoais).
- Logs não incluem token nem conteúdo das conversas.
- O bot ignora mensagens de chats/usuários fora da allowlist.

## Operação e Deploy

### 1. Serviço no Linux com systemd

Crie o arquivo `/etc/systemd/system/canvas-assistant.service`:

```ini
[Unit]
Description=Assistente Acadêmico PUC Minas Canvas
After=network.target

[Service]
Type=simple
User=seu_usuario
WorkingDirectory=/caminho/para/puc-canvas-assistant
ExecStart=/home/seu_usuario/.local/bin/uv run python -m canvas_assistant run
Restart=always
RestartSec=10
EnvironmentFile=/caminho/para/puc-canvas-assistant/.env

# Limites e proteção de recursos
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Ativação e verificação:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now canvas-assistant.service
sudo systemctl status canvas-assistant.service
```

### 2. Serviço no Windows

Opção A: **NSSM (Non-Sucking Service Manager)**:
```powershell
nssm install CanvasAssistant "C:\Users\<Usuario>\.cargo\bin\uv.exe" "run python -m canvas_assistant run"
nssm set CanvasAssistant AppDirectory "C:\caminho\para\puc-canvas-assistant"
nssm set CanvasAssistant AppRestartDelay 10000
nssm start CanvasAssistant
```

Opção B: **Agendador de Tarefas do Windows (Task Scheduler)**:
- Criar tarefa com disparo "Ao inicializar o sistema" ou "Ao fazer logon".
- Ação: Iniciar programa `uv.exe` com argumentos `run python -m canvas_assistant run`.
- Diretório de trabalho: pasta raiz do repositório.
- Configurar para reiniciar automaticamente em caso de falha.

### 3. Backup Automatizado do Banco de Dados

O banco de dados SQLite (`data/assistant.sqlite3`) pode ser copiado em tempo de execução de forma consistente via API `sqlite3.backup`:

```bash
# Executa backup com rotação (mantém 7 cópias mais recentes em data/backups/)
uv run python -m canvas_assistant backup

# Verifica a integridade atômica do backup
uv run python -m canvas_assistant backup-check
```

Para automatizar, adicione uma entrada diária no cron (Linux) ou no Agendador de Tarefas (Windows):
```cron
0 3 * * * cd /caminho/puc-canvas-assistant && uv run python -m canvas_assistant backup
```

### 4. Rotação de Logs e Redação de Segredos

- Os logs são gravados em `data/logs/assistant.log` com rotação automática (`RotatingFileHandler`, 5 MB por arquivo, até 3 cópias de backup).
- Tokens e chaves sensíveis (`CANVAS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, cabeçalhos `Bearer`) são redigidos automaticamente como `***REDACTED***` antes de serem gravados em disco ou exibidos no console.

### 5. Monitoramento e Alertas de Incidentes

O assistente monitora continuamente:
- **Canvas 401/403 (token inválido ou expirado):** emite alerta imediato no Telegram uma única vez.
- **Canvas indisponível:** emite alerta caso o Canvas permaneça fora do ar por mais de 10 minutos (configurável via `INCIDENT_CANVAS_DOWN_MINUTES`).
- **Falhas consecutivas no LLM:** emite alerta após 3 falhas consecutivas (configurável via `INCIDENT_LLM_FAILURES`).
- **Recuperação automática:** assim que o serviço afetado volta a responder com sucesso, uma mensagem notificando a normalização é enviada.
- **Diagnóstico ao vivo:** envie `/saude` no Telegram para obter o relatório completo de status operacional.

### 6. Catch-up ao iniciar e Heartbeat externo

**Catch-up (Etapa 4.1.2):** o bot persiste o horário do último ciclo do scheduler concluído em
`schema_meta` (`scheduler:last_cycle:<user_id>`). Ao iniciar (ou quando detecta uma lacuna maior que
`CATCHUP_GAP_MINUTES`, padrão 30), o bot roda o sync imediatamente e envia **uma** mensagem
consolidada: "Fiquei offline de X a Y. Pendências: ..." com tarefas que venceram na lacuna, as que
vencem nas próximas 24 h, avisos e arquivos novos. A janela de silêncio vale, exceto para prazos em
menos de `CATCHUP_URGENT_HOURS` (padrão 6). A mensagem é deduplicada com os lembretes normais via
`notification_events`, evitando inundações (uma mensagem por item) ou descarte silencioso.

**Heartbeat externo (Etapa 4.1.2):** configure `HEARTBEAT_URL` (serviços tipo healthchecks.io)
e `HEARTBEAT_INTERVAL_MINUTES` (padrão 10). O bot faz um GET a cada intervalo **somente se o último
ciclo do scheduler foi bem-sucedido**. Sem a variável, não faz nada. No healthchecks.io:

1. Crie um check (ex.: `canvas-assistant-heartbeat`).
2. Copie a Ping URL (ex.: `https://hc-ping.com/<uuid>`).
3. Defina `HEARTBEAT_URL` no `.env` com essa URL.
4. Configure o alerta do serviço (e-mail ou Telegram — o healthchecks.io tem um bot
   `@healthchecks_bot`) para avisar quando o bot parar de fazer pings.
5. Defina o período de graça do healthchecks para `HEARTBEAT_INTERVAL_MINUTES * 2` minutos.

## Desenvolvimento

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check src tests
```

Os testes cobrem paginação (incluindo a regressão em que o link `next` perdia a query),
retry/429/401, parsing dos dados do Canvas, repositório, ferramentas, loop do agente,
grounding/URLs inventadas, formatação HTML, timezone, divisão de mensagens do Telegram
e o serviço de notificações (baseline, dedupe, quiet hours, lembretes e digest).

## Limitações da Fase 2

- Uso pessoal/single-user; a base já separa dados por `user_id`.
- Notificações dependem de uma sincronização bem-sucedida e recente; se o Canvas
  falhar por muito tempo, os envios ficam pausados.
- Arquivos em matérias sem aba `Arquivos` dependem dos módulos; erros 403 viram
  "falhas recentes" no `/status`.
- Links de arquivos abrem no Canvas; o assistente não baixa nem resume PDFs ainda.
- Próximas fases: base de conhecimento com RAG, tutor socrático e adaptador de WhatsApp.
