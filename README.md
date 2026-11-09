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
- Bot do Telegram restrito ao seu usuário/chat, com comandos `/sincronizar` (ou `/sync`), `/arquivos <matéria>`, `/status`, `/testnotify` e `/help`.
- Cache SQLite local com atualização automática (Canvas a cada 20 min; download de arquivos a cada 1 hora).
- Agente LLM com ferramentas para consultar o cache, contexto de hoje/amanhã e validação de links contra os dados retornados (anti-alucinação).
- Respostas em HTML seguro, com negrito, itálico, código e links clicáveis.
- CLI para validar configuração, sincronizar dados, baixar arquivos, listar arquivos conhecidos, perguntar e ver status.
- LLM local via Ollama (`qwen3:14b`) ou APIs compatíveis com OpenAI (OpenAI, Grok, Gemini).

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

# inicia o bot (sincroniza e depois faz refresh a cada 20 min)
uv run python -m canvas_assistant run

# estado do cache e falhas recentes
uv run python -m canvas_assistant status
```

No Telegram:

- `o que tenho pra hoje?`
- `como estão minhas notas?`
- `quais matérias estou cursando?`
- `tem aviso novo?`
- `quais arquivos tem em Cálculo?`
- `/sincronizar` (ou `/sync`), `/arquivos <matéria>`, `/status`, `/testnotify`, `/help`

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
