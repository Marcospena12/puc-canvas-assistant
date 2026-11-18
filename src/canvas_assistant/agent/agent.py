from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections import deque
from zoneinfo import ZoneInfo

from ..config import UserProfile
from ..knowledge.study_context import (
    DEFAULT_STUDY_GROUNDING_MIN_OVERLAP,
    post_process_study_answer,
)
from ..knowledge.task_briefing import format_refusal_message
from ..llm.base import LLMProvider, Message
from ..repository import Repository
from ..timeutil import local_now
from .context import Evidence, build_context_pack
from .grounding import (
    ToolObservation,
    build_evidence,
    correction_message,
    deterministic_fallback,
    parse_tool_result,
    validate_answer,
)
from .prompts import build_system_prompt
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

FALLBACK_ANSWER = "Não consegui concluir a consulta agora. Tente de novo ou use /sync."
UNCONFIRMED_ANSWER = (
    "Não consegui confirmar os dados dessa resposta. Tente perguntar de novo ou use /sync."
)

ROUTER_DEADLINE_KEYWORDS = (
    "prazo",
    "prazos",
    "entrega",
    "entregas",
    "entregar",
    "hoje",
    "amanha",
    "amanhã",
    "semana",
    "mes",
    "mês",
    "pendente",
    "pendentes",
    "tarefa",
    "tarefas",
    "trabalho",
    "trabalhos",
    "prova",
    "provas",
    "atividade",
    "atividades",
)

ROUTER_GRADE_KEYWORDS = (
    "nota",
    "notas",
    "pontos",
    "pontuacao",
    "pontuação",
    "desempenho",
    "media",
    "média",
    "boletim",
)

ROUTER_COURSE_KEYWORDS = (
    "materia",
    "materias",
    "matéria",
    "matérias",
    "disciplina",
    "disciplinas",
    "curso",
    "cursos",
    "professor",
    "professores",
    "professora",
    "grade",
)

ROUTER_CONVERSATIONAL_PREFIXES = (
    "oi",
    "ola",
    "olá",
    "bom dia",
    "boa tarde",
    "boa noite",
    "obrigado",
    "obrigada",
    "valeu",
    "tchau",
    "ate mais",
    "até mais",
    "ajuda",
)

ROUTER_ANNOUNCEMENT_KEYWORDS = (
    "aviso",
    "avisos",
    "recado",
    "recados",
    "comunicado",
    "comunicados",
)

ROUTER_TUTOR_REFUSE_PATTERNS = (
    "resolve pra mim",
    "resolva pra mim",
    "faz pra mim",
    "faca pra mim",
    "responde pra mim",
    "responda pra mim",
    "faz o trabalho",
    "faca o trabalho",
    "faz a tarefa",
    "faca a tarefa",
    "escreve pra mim",
    "escreva pra mim",
    "completa pra mim",
    "complete pra mim",
    "me da a resposta",
    "me entrega o trabalho",
    "faz a atividade",
    "faca a atividade",
    "resolva a atividade",
    "resolve a atividade",
    "me da o trabalho",
    "me da a resposta da questao",
    "me da a resposta da atividade",
)


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in folded if not unicodedata.combining(c)).casefold().strip()


def classify_message_intent(text: str) -> str:
    """Classifica a mensagem por regras simples e explícitas em:

    'deadlines', 'grades', 'courses', 'conversational', 'announcements',
    'tutor_refuse' ou 'study'.

    Ordem das regras:
    1. conversational (saudações curtas, <=4 palavras)
    2. tutor_refuse ("resolve/faz/responde pra mim" + atividade/trabalho)
    3. announcements ("aviso/avisos/recado/comunicado")
    4. grades (nota/média/pontos/desempenho, "quanto falta...passar", "quanto...tirei")
    5. deadlines (prazo/entrega/pendente/limite, "atrasado", "quando...prova/trabalho",
       "o que/quais...hoje/semana", ou pergunta com "hoje/semana" + temporal)
    6. courses (matéria/disciplina/professor, exceto "o que o material/no material/explica")
    7. study (fallback default)

    Exceção notável: "como funciona a avaliação da disciplina?" → courses, porque
    "disciplina" dispara course_terms e a exceção do study ("o que o material /
    no material / explica") não casa com "como funciona a avaliação". A regra
    "como funciona → study" (definition_query) só vale quando não há termo de
    matéria/disciplina presente.
    """
    clean = _normalize(text)
    words = set(re.findall(r"\b\w+\b", clean))
    clean_no_punct = re.sub(r"[^\w\s]", " ", clean)
    clean_norm = re.sub(r"\s+", " ", clean_no_punct).strip()

    matches_conv = any(
        clean_norm == p or clean_norm.startswith(f"{p} ") for p in ROUTER_CONVERSATIONAL_PREFIXES
    )
    if matches_conv and len(words) <= 4:
        logger.info(
            "Roteador: regra=conversational(prefixo) intent=conversational para: %s",
            text[:80],
        )
        return "conversational"
    if any(k in words for k in ("obrigado", "obrigada", "valeu", "tchau")) and len(words) <= 4:
        logger.info(
            "Roteador: regra=conversational(agradecimento) intent=conversational para: %s",
            text[:80],
        )
        return "conversational"

    # 2. tutor_refuse: "resolve/faz/responde pra mim" + atividade/trabalho
    tutor_refuse_norm = clean_norm.replace("atividade", "atividade").replace("trabalho", "trabalho")
    if any(p in tutor_refuse_norm for p in ROUTER_TUTOR_REFUSE_PATTERNS):
        logger.info("Roteador: regra=tutor_refuse intent=tutor_refuse para: %s", text[:80])
        return "tutor_refuse"

    # 3. announcements: "aviso/avisos/recado/comunicado"
    if any(k in words for k in ROUTER_ANNOUNCEMENT_KEYWORDS):
        logger.info("Roteador: regra=announcements intent=announcements para: %s", text[:80])
        return "announcements"

    definition_query = any(
        marker in clean_norm
        for marker in (
            "o que e ",
            "o que sao ",
            "o que significa",
            "como funciona",
            "defina ",
            "definicao de",
            "conceito de",
        )
    )

    # 4. grades
    grade_terms = ("nota", "notas", "boletim", "desempenho", "media")
    if not definition_query and (
        any(k in words for k in grade_terms)
        or ("quanto falta" in clean_norm and "passar" in words)
        or ("quanto" in words and "tirei" in words)
    ):
        logger.info("Roteador: regra=grades intent=grades para: %s", text[:80])
        return "grades"

    # 5. deadlines
    deadline_terms = (
        "prazo",
        "prazos",
        "entrega",
        "entregas",
        "entregar",
        "pendente",
        "pendentes",
        "limite",
    )
    if any(k in words for k in deadline_terms) or "data limite" in clean_norm:
        logger.info("Roteador: regra=deadlines(termo) intent=deadlines para: %s", text[:80])
        return "deadlines"
    if any(k in words for k in ("atrasado", "atrasada", "atrasados", "atrasadas", "atraso")):
        logger.info("Roteador: regra=deadlines(atrasado) intent=deadlines para: %s", text[:80])
        return "deadlines"
    if "quando" in words and any(
        k in words
        for k in (
            "prova",
            "provas",
            "trabalho",
            "trabalhos",
            "atividade",
            "atividades",
            "entrega",
            "entregas",
        )
    ):
        logger.info("Roteador: regra=deadlines(quando) intent=deadlines para: %s", text[:80])
        return "deadlines"
    if ("o que" in clean or "quais" in clean) and any(
        k in words for k in ("hoje", "amanha", "semana")
    ):
        logger.info(
            "Roteador: regra=deadlines(o que/quais+temporal) intent=deadlines para: %s",
            text[:80],
        )
        return "deadlines"
    # "tem algo pra hoje?", "tem trabalho pra semana que vem?" — pergunta com temporal
    temporal_question = any(k in words for k in ("hoje", "amanha", "semana", "mes", "proximo"))
    task_terms = (
        "trabalho",
        "trabalhos",
        "atividade",
        "atividades",
        "prova",
        "provas",
        "algo",
        "pendente",
        "pendentes",
    )
    if temporal_question and any(k in words for k in task_terms):
        logger.info(
            "Roteador: regra=deadlines(temporal+tarefa) intent=deadlines para: %s",
            text[:80],
        )
        return "deadlines"

    # 6. courses
    course_terms = ("materia", "materias", "disciplina", "disciplinas", "professor", "professores")
    if any(k in words for k in course_terms) and not any(
        k in clean for k in ("o que o material", "no material", "explica")
    ):
        logger.info("Roteador: regra=courses intent=courses para: %s", text[:80])
        return "courses"

    logger.info("Roteador: regra=study(fallback) intent=study para: %s", text[:80])
    return "study"


class Agent:
    def __init__(
        self,
        provider: LLMProvider,
        repo: Repository,
        user: UserProfile,
        tz: ZoneInfo,
        *,
        max_tool_rounds: int = 6,
        history_turns: int = 6,
    ) -> None:
        self.provider = provider
        self.repo = repo
        self.user = user
        self.tz = tz
        self.max_tool_rounds = max_tool_rounds
        self.tools = ToolRegistry(repo, user, tz)
        self._history: dict[str, deque[tuple[str, str]]] = {}
        self._history_turns = history_turns
        self._last_evidence: dict[str, Evidence] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self) -> asyncio.Lock:
        lock = self._locks.get(self.user.user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[self.user.user_id] = lock
        return lock

    def _system_message(self) -> tuple[Message, frozenset[str]]:
        now = local_now(self.tz)
        courses = self.repo.get_courses(
            self.user.user_id, current_only=True, include_non_subjects=False
        )
        context = build_context_pack(self.repo, self.user, self.tz, now)
        evidence = self._last_evidence.get(self.user.user_id)
        prompt = build_system_prompt(now, self.tz.key, courses, context, evidence)
        allowed = set(context.urls)
        if evidence:
            allowed.update(evidence.urls)
        return Message(role="system", content=prompt), frozenset(allowed)

    def _remember(self, user_text: str, answer: str) -> None:
        history = self._history.setdefault(self.user.user_id, deque(maxlen=self._history_turns))
        history.append((user_text, answer))

    def _finalize(
        self,
        user_text: str,
        answer: str,
        observations: list[ToolObservation],
    ) -> str:
        if observations:
            self._last_evidence[self.user.user_id] = build_evidence(observations)
        self._remember(user_text, answer)
        return answer

    def _previous_messages(self) -> list[Message]:
        history = self._history.get(self.user.user_id)
        if not history:
            return []
        messages: list[Message] = []
        for user_text, answer in history:
            messages.append(Message(role="user", content=user_text))
            messages.append(Message(role="assistant", content=answer))
        return messages

    async def reply(self, text: str) -> str:
        t0 = time.perf_counter()
        total_in = 0
        total_out = 0
        async with self._lock():
            if self.repo.is_empty(self.user.user_id):
                return "Ainda não tenho dados do Canvas. Rode /sync e tente novamente."

            system, grounded_urls = self._system_message()
            messages = [
                system,
                *self._previous_messages(),
                Message(role="user", content=text),
            ]
            allowed_urls = set(grounded_urls)
            observations: list[ToolObservation] = []
            correction_sent = False

            intent = classify_message_intent(text)
            study_sources_map: dict[str, tuple[str, ...]] = {}
            study_coverage = "none"
            study_context_texts: list[str] = []

            # Rota de política de tutor: "resolve/faz/responde pra mim" → recusa
            if intent == "tutor_refuse":
                refusal = format_refusal_message("", "")
                self._record_usage(t0, 0, 0)
                self._remember(text, refusal)
                return refusal

            # Rota de avisos: "tem aviso novo?" → get_announcements determinístico
            if intent == "announcements":
                arguments = {"limit": 5}
                raw = await self.tools.execute("get_announcements", arguments)
                observation = parse_tool_result("get_announcements", arguments, raw)
                observations.append(observation)
                allowed_urls.update(observation.urls)
                obs_data = observation.data or {}
                ann_rows = obs_data.get("announcements") or []
                if not ann_rows:
                    answer = "Não há avisos novos no Canvas no momento."
                else:
                    lines = ["Avisos recentes:"]
                    for row in ann_rows[:10]:
                        parts = [
                            row.get("course", ""),
                            row.get("title", ""),
                        ]
                        link = row.get("url")
                        if link:
                            parts.append(f"[abrir]({link})")
                        lines.append("- " + " | ".join(p for p in parts if p))
                    answer = "\n".join(lines)
                self._record_usage(t0, 0, 0)
                return self._finalize(text, answer, observations)

            if intent == "study":
                arguments = {"query": text, "k": 5}
                raw = await self.tools.execute("search_course_material", arguments)
                observation = parse_tool_result("search_course_material", arguments, raw)
                observations.append(observation)
                allowed_urls.update(observation.urls)
                obs_data = observation.data or {}
                study_coverage = str(obs_data.get("coverage") or "none")
                blocks = obs_data.get("blocks") or []
                study_context_texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
                for b in blocks:
                    if isinstance(b, dict) and b.get("block_id"):
                        key = f"[{b['block_id']}]"
                        study_sources_map[key] = (
                            b.get("course", ""),
                            b.get("file", ""),
                            b.get("location", ""),
                            b.get("text", ""),
                        )
                logger.info(
                    "Roteador: classificado como 'estudo' (coverage=%s, blocos=%d) para: %s",
                    study_coverage,
                    len(blocks),
                    text[:80],
                )
                messages.append(
                    Message(
                        role="system",
                        content=(
                            "Contexto do acervo recuperado antes da resposta (estudo):\n"
                            "Use as referências [Fn] para fundamentar toda afirmação de conteúdo.\n"
                            "Se coverage for 'weak' ou 'none', informe na primeira linha que o "
                            "assunto não foi localizado no material antes de qualquer "
                            "complemento.\n\n"
                            f"{raw}"
                        ),
                    )
                )

            try:
                for _ in range(self.max_tool_rounds):
                    response = await self.provider.chat(messages, self.tools.specs)
                    total_in += int(getattr(response, "input_tokens", 0) or 0)
                    total_out += int(getattr(response, "output_tokens", 0) or 0)
                    if response.tool_calls:
                        messages.append(
                            Message(
                                role="assistant",
                                content=response.text or "",
                                tool_calls=response.tool_calls,
                            )
                        )
                        for call in response.tool_calls:
                            result = await self.tools.execute(call.name, call.arguments)
                            observation = parse_tool_result(call.name, call.arguments, result)
                            observations.append(observation)
                            allowed_urls.update(observation.urls)
                            ok = bool(observation.data and observation.data.get("ok"))
                            logger.info("Ferramenta %s: %s", call.name, "ok" if ok else "erro")
                            messages.append(
                                Message(
                                    role="tool",
                                    content=result,
                                    tool_call_id=call.id,
                                    tool_name=call.name,
                                )
                            )
                        continue

                    answer = (response.text or "").strip()
                    if not answer:
                        fallback = deterministic_fallback(observations)
                        if fallback:
                            return self._finalize_with_usage(
                                text, fallback, observations, t0, total_in, total_out
                            )
                        answer = "Não consegui montar uma resposta. Tente reformular a pergunta."
                        return self._finalize_with_usage(
                            text, answer, observations, t0, total_in, total_out
                        )

                    unknown = validate_answer(answer, allowed_urls)
                    if not unknown:
                        if intent == "study" and study_sources_map:
                            answer = post_process_study_answer(
                                answer,
                                study_sources_map,
                                min_overlap=DEFAULT_STUDY_GROUNDING_MIN_OVERLAP,
                                coverage=study_coverage,
                                context_texts=study_context_texts,
                            )
                        return self._finalize_with_usage(
                            text, answer, observations, t0, total_in, total_out
                        )

                    logger.warning(
                        "Resposta com dados não confirmados (%s); solicitando correção",
                        ", ".join(sorted(unknown)[:3]),
                    )
                    if correction_sent:
                        fallback = deterministic_fallback(observations)
                        if fallback:
                            return self._finalize_with_usage(
                                text, fallback, observations, t0, total_in, total_out
                            )
                        return self._finalize_with_usage(
                            text, UNCONFIRMED_ANSWER, observations, t0, total_in, total_out
                        )

                    correction_sent = True
                    messages.append(Message(role="assistant", content=answer))
                    messages.append(Message(role="system", content=correction_message(unknown)))
            except Exception:
                logger.exception("Falha ao consultar o LLM ou executar ferramentas")
                fallback = deterministic_fallback(observations)
                if fallback:
                    return self._finalize_with_usage(
                        text, fallback, observations, t0, total_in, total_out
                    )

            self._record_usage(t0, total_in, total_out)
            self._remember(text, FALLBACK_ANSWER)
            return FALLBACK_ANSWER

    def _record_usage(self, t0: float, total_in: int, total_out: int) -> None:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        try:
            self.repo.record_study_usage(
                self.user.user_id,
                "agente",
                input_tokens=total_in,
                output_tokens=total_out,
                context_blocks=0,
                latency_ms=latency_ms,
            )
        except Exception:
            logger.debug("Falha ao registrar uso do agente", exc_info=True)

    def _finalize_with_usage(
        self,
        user_text: str,
        answer: str,
        observations: list[ToolObservation],
        t0: float,
        total_in: int,
        total_out: int,
    ) -> str:
        self._record_usage(t0, total_in, total_out)
        return self._finalize(user_text, answer, observations)

    async def aclose(self) -> None:
        await self.provider.aclose()
