# Assistente Canvas PUC Minas

Assistente acadêmico pessoal que lê os dados do Canvas LMS (somente leitura),
guarda um cache local e responde perguntas em linguagem natural pelo Telegram
ou pelo terminal.

## O que já funciona (Fase 1)

- Sincronização de cursos, tarefas, prazos do planner, avisos, arquivos e módulos.
- Cache SQLite local com atualização automática a cada 20 minutos.
- Agente LLM com ferramentas para consultar o cache (sem inventar dados).
- Bot do Telegram restrito ao seu usuário/chat, com comandos `/sync`, `/status` e `/help`.
- CLI para validar configuração, sincronizar, perguntar e ver o status.
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

## Uso

```powershell
# valida Canvas, banco e modelo local
uv run python -m canvas_assistant check

# baixa os dados do Canvas para o cache local
uv run python -m canvas_assistant sync

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
- `/sync`, `/status`, `/help`

O agente só responde a perguntas de tutoria. Ele não resolve quizzes nem
entrega tarefas: integridade acadêmica é uma regra do prompt e do escopo do projeto.

## Arquitetura

```text
src/canvas_assistant/
├── config.py          # Settings, perfil do usuário e configuração do LLM
├── db.py              # schema SQLite (v1)
├── repository.py      # upserts e consultas usadas pelas ferramentas
├── canvas/
│   ├── client.py      # HTTP read-only, paginação, retry, rate limit
│   ├── service.py     # endpoints de domínio do Canvas
│   └── errors.py      # erros tipados (401, 403, 404, 429, 5xx)
├── sync.py            # orquestra a sincronização e isola falhas por curso/aba
├── agent/
│   ├── agent.py       # loop de tool calling e histórico por usuário
│   ├── prompts.py     # prompt de sistema em pt-BR
│   └── tools.py       # 6 ferramentas + validação com pydantic
├── llm/               # Ollama nativo e APIs compatíveis com OpenAI
├── channels/          # interface de canal + adaptador do Telegram
├── app.py             # bot, scheduler de refresh e fiação geral
└── cli.py             # run, check, sync, ask, status
```

Fluxo resumido:

1. `sync` consulta as abas do Canvas, pagina os endpoints e grava tudo no SQLite.
2. A cada 20 minutos o scheduler executa a mesma sincronização (sem sobreposição).
3. Uma mensagem do Telegram vira uma pergunta ao LLM com as 6 ferramentas disponíveis.
4. O LLM escolhe ferramentas; elas leem apenas o cache local e devolvem JSON.
5. O LLM formula a resposta final; o bot envia em partes de até 4096 caracteres.

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
timezone e divisão de mensagens do Telegram.

## Limitações da Fase 1

- Uso pessoal/single-user; a base já separa dados por `user_id`.
- Notificações proativas ainda não são enviadas (o refresh apenas atualiza o cache).
- Arquivos em matérias sem aba `Arquivos` dependem dos módulos; erros 403 viram
  "falhas recentes" no `/status`.
- Links de arquivos abrem no Canvas; o assistente não baixa nem resume PDFs ainda.
- Próximas fases: notificações de prazos/avisos, base de conhecimento com RAG,
  tutor socrático e adaptador de WhatsApp.
