"""Revisão de rascunho pelo tutor (Etapa B 4.2).

O bot AVALIA o rascunho contra os critérios da tarefa e APONTA lacunas.
NÃO reescreve seções, NÃO gera parágrafos substitutos, NÃO interage com quizzes
e NÃO escreve no Canvas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings, UserProfile
from ..llm.base import LLMProvider, Message
from ..repository import Repository
from .task_briefing import is_quiz_task, resolve_task_selection

logger = logging.getLogger(__name__)

REVIEW_PROMPT_VERSION = "v1"
REVIEW_DRAFT_TEXT_MAX_CHARS = 4000
_REVIEW_ALLOWED_FILE_EXTS = frozenset({".docx", ".pdf", ".txt", ".md"})


@dataclass(slots=True)
class ReviewSession:
    user_id: str
    assignment_id: int
    assignment_name: str
    course_name: str
    started_at: datetime
    expires_at: datetime
    rubric: list[dict[str, Any]] = field(default_factory=list)
    description_html: str = ""
    cloud_confirmed: bool = False

    def is_active(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        return current < self.expires_at


@dataclass(slots=True)
class ReviewResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    from_cache: bool = False
    cloud_used: bool = False


class ReviewService:
    """Gerencia sessões de revisão e gera feedback por critério."""

    def __init__(
        self,
        repo: Repository,
        user: UserProfile,
        tz: ZoneInfo,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.user = user
        self.tz = tz
        self.settings = settings
        self._sessions: dict[str, ReviewSession] = {}

    def _session_key(self, user_id: str) -> str:
        return user_id

    def start_session(
        self, assignment: dict[str, Any], now: datetime | None = None
    ) -> ReviewSession:
        current = now or datetime.now(UTC)
        minutes = self.settings.review_session_minutes
        session = ReviewSession(
            user_id=self.user.user_id,
            assignment_id=int(assignment["id"]),
            assignment_name=assignment.get("name") or "",
            course_name=assignment.get("course_name") or "",
            started_at=current,
            expires_at=current + timedelta(minutes=minutes),
            rubric=json.loads(assignment.get("rubric_json") or "[]"),
            description_html=assignment.get("description_html") or "",
        )
        self._sessions[self._session_key(self.user.user_id)] = session
        return session

    def get_session(self, now: datetime | None = None) -> ReviewSession | None:
        session = self._sessions.get(self._session_key(self.user.user_id))
        if session is None:
            return None
        if not session.is_active(now):
            self._sessions.pop(self._session_key(self.user.user_id), None)
            return None
        return session

    def cancel_session(self) -> bool:
        return self._sessions.pop(self._session_key(self.user.user_id), None) is not None

    def resolve_task(self, query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        return resolve_task_selection(self.repo, self.user.user_id, query, self.tz)

    def is_quiz(self, assignment: dict[str, Any]) -> bool:
        return is_quiz_task(assignment)

    def extract_text_from_file(self, file_path: Path) -> str:
        """Extrai texto de .docx/.pdf/.txt/.md e apaga o arquivo logo após."""
        ext = file_path.suffix.lower()
        text = ""
        try:
            if ext == ".pdf":
                text = _extract_pdf(file_path)
            elif ext == ".docx":
                text = _extract_docx(file_path)
            elif ext in (".txt", ".md"):
                text = file_path.read_text(encoding="utf-8", errors="replace")
            else:
                raise ValueError(f"Tipo de arquivo não suportado: {ext}")
        finally:
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                logger.warning("Falha ao apagar arquivo temporário %s", file_path)
        if self.settings.review_persist_drafts is False:
            logger.info("Rascunho processado e descartado (REVIEW_PERSIST_DRAFTS=false)")
        return text

    def validate_file(self, file_path: Path, max_size_mb: int) -> str | None:
        """Valida tipo e tamanho. Retorna None se OK, ou mensagem de erro."""
        ext = file_path.suffix.lower()
        if ext not in _REVIEW_ALLOWED_FILE_EXTS:
            return f"Tipo de arquivo não aceito: {ext}. Use .docx, .pdf, .txt ou .md."
        size = file_path.stat().st_size
        max_bytes = max_size_mb * 1024 * 1024
        if size > max_bytes:
            return f"Arquivo muito grande: {size / (1024 * 1024):.1f} MB. Limite: {max_size_mb} MB."
        return None

    def cloud_confirmation_needed(self) -> bool:
        """Retorna True se o provider for na nuvem e precisa de confirmação."""
        provider = self.settings.llm_provider
        return provider in ("anthropic", "openai", "grok", "gemini")

    def build_catchup_message_intro(self, session: ReviewSession) -> str:
        """Primeira mensagem da sessão: avisa destino do texto e pede confirmação se nuvem."""
        provider = self.settings.llm_provider
        if provider == "ollama":
            dest = "modelo local (Ollama) — seu rascunho não sai do seu computador."
        else:
            dest = f"provedor em nuvem ({provider}). O texto do rascunho será enviado para a API."
        lines = [
            f"📋 Modo de revisão: {session.assignment_name} ({session.course_name})",
            f"O texto do rascunho será processado por um {dest}",
        ]
        if self.cloud_confirmation_needed() and not session.cloud_confirmed:
            lines.append(
                "Para revisar, confirme enviando 'sim' (ou envie o rascunho como texto/arquivo)."
            )
        else:
            lines.append(
                "Envie o rascunho como texto (até 4000 caracteres) "
                "ou como arquivo .docx, .pdf, .txt ou .md (até 10 MB)."
            )
        lines.append("Use /revisar cancelar para encerrar a sessão.")
        return "\n".join(lines)

    async def review_draft(
        self,
        provider: LLMProvider,
        session: ReviewSession,
        draft_text: str,
    ) -> ReviewResult:
        """Gera feedback por critério, validando citações no código."""
        max_chars = self.settings.review_max_chars
        if len(draft_text) > max_chars:
            return ReviewResult(
                text=(
                    f"Rascunho muito longo ({len(draft_text)} caracteres). "
                    f"Envie por seções ou faixa de páginas (limite: {max_chars})."
                )
            )

        draft_hash = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
        rubric_str = json.dumps(session.rubric, sort_keys=True, ensure_ascii=False)
        rubric_hash = hashlib.sha256(rubric_str.encode("utf-8")).hexdigest()
        cache_key = (
            f"review:{session.assignment_id}:{draft_hash}:{rubric_hash}:{REVIEW_PROMPT_VERSION}"
        )

        cached = self.repo.get_meta(cache_key)
        if cached:
            logger.info("Review cache hit: %s", cache_key[:40])
            return ReviewResult(text=cached, from_cache=True)

        rubric_text = _format_rubric_for_prompt(session.rubric, session.description_html)
        formal_checks = _extract_formal_checks(session.description_html)
        word_count = len(draft_text.split())

        system_prompt = _build_review_system_prompt()
        user_prompt = _build_review_user_prompt(
            session.assignment_name,
            session.course_name,
            rubric_text,
            formal_checks,
            draft_text,
            word_count,
        )

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ]

        t0 = time.perf_counter()
        response = await provider.chat(messages, tools=[])
        latency_ms = int((time.perf_counter() - t0) * 1000)

        raw_feedback = response.text or ""

        feedback = _validate_and_format_feedback(
            raw_feedback, draft_text, formal_checks, word_count
        )

        self.repo.set_meta(cache_key, feedback)
        self.repo.record_study_usage(
            self.user.user_id,
            "revisar",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            context_blocks=0,
            latency_ms=latency_ms,
        )
        logger.info(
            "Review LLM: %sms | in_tok=%s out_tok=%s | draft_chars=%s words=%s",
            latency_ms,
            response.input_tokens,
            response.output_tokens,
            len(draft_text),
            word_count,
        )

        return ReviewResult(
            text=feedback,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=latency_ms,
            from_cache=False,
            cloud_used=self.cloud_confirmation_needed(),
        )


def _normalize_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "")
    clean = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    return re.sub(r"\s+", " ", clean).strip()


def _extract_pdf(path: Path) -> str:
    import fitz

    doc = fitz.open(str(path))
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _extract_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _format_rubric_for_prompt(rubric: list[dict[str, Any]], description: str) -> str:
    if rubric:
        lines = ["Rubrica oficial:"]
        for item in rubric:
            desc = item.get("description") or item.get("description_html") or ""
            pts = item.get("points", "?")
            lines.append(
                f"- {item.get('description', item.get('id', '?'))} ({pts} pts): {desc[:200]}"
            )
        return "\n".join(lines)
    if description:
        return (
            "Sem rubrica oficial. Critérios derivados do enunciado (sem rubrica oficial):\n"
            f"{description[:2000]}"
        )
    return "Sem rubrica e sem enunciado. Fazer apenas checagem formal."


def _extract_formal_checks(description: str) -> dict[str, Any]:
    """Extrai requisitos formais do enunciado (limite de palavras/páginas, template)."""
    checks: dict[str, Any] = {}
    if not description:
        return checks
    word_match = re.search(
        r"(\d+)\s*(?:palavras|words|páginas|pages|laudas)\b",
        description,
        re.IGNORECASE,
    )
    if word_match:
        checks["word_limit"] = int(word_match.group(1))
        checks["word_unit"] = "palavras" if "palavra" in word_match.group(0).lower() else "páginas"
    template_match = re.search(r"template\s+([\w-]+)", description, re.IGNORECASE)
    if template_match:
        checks["template"] = template_match.group(1)
    return checks


def _build_review_system_prompt() -> str:
    return (
        "Você é um tutor acadêmico que AVALIA rascunhos contra os critérios da tarefa.\n"
        "REGRAS INVIOLÁVEIS:\n"
        "1. NUNCA reescreva seções do rascunho. NUNCA gere parágrafos substitutos.\n"
        "2. Se o aluno pedir 'reescreve', 'melhora o texto' ou 'completa pra mim', recuse "
        "em uma frase e ofereça apontar o que melhorar.\n"
        "3. Para cada critério, dê:\n"
        "   - nível: Atende / Atende parcialmente / Não identificado\n"
        "   - evidência: citação LITERAL curta do rascunho (até 25 palavras) com localização\n"
        "   - o que falta: observação ou pergunta ligada ao enunciado ou acervo\n"
        "4. NUNCA dê nota ou pontuação estimada.\n"
        "5. No fim, liste até 3 prioridades de ajuste em ordem.\n"
        "6. Termine com: 'estimativa automática, não substitui a avaliação do professor'.\n"
        "Responda em JSON com a estrutura:\n"
        '{"criterios": [{"nome": "...", "nivel": "...", "evidencia": "...", '
        '"localizacao": "...", "o_que_falta": "..."}], "prioridades": ["...", "..."], '
        '"aviso": "estimativa automática, não substitui a avaliação do professor"}'
    )


def _build_review_user_prompt(
    task_name: str,
    course_name: str,
    rubric_text: str,
    formal_checks: dict[str, Any],
    draft_text: str,
    word_count: int,
) -> str:
    checks_text = json.dumps(formal_checks, ensure_ascii=False) if formal_checks else "{}"
    return (
        f"Tarefa: {task_name}\n"
        f"Matéria: {course_name}\n"
        f"Critérios:\n{rubric_text}\n\n"
        f"Checagens formais do enunciado (JSON): {checks_text}\n"
        f"Contagem de palavras do rascunho: {word_count}\n\n"
        f"RASCUNHO DO ALUNO:\n{draft_text}\n\n"
        "Avalie o rascunho contra cada critério. Para a evidência, cite trecho LITERAL "
        "do rascunho (até 25 palavras) e diga o parágrafo (P1, P2, ...) ou página. "
        "Se não houver evidência, marque 'sem evidência verificável'."
    )


def _validate_and_format_feedback(
    raw: str,
    draft_text: str,
    formal_checks: dict[str, Any],
    word_count: int,
) -> str:
    """Valida citações no código e formata feedback em HTML seguro para Telegram."""
    draft_norm = _normalize_text(draft_text)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = {"criterios": [], "prioridades": [], "aviso": "", "raw": raw[:500]}

    lines = ["📋 <b>Revisão do rascunho</b>"]
    criterios = data.get("criterios") or []
    for crit in criterios:
        nome = crit.get("nome") or "Critério"
        nivel = crit.get("nivel") or "Não identificado"
        evidencia = crit.get("evidencia") or ""
        localizacao = crit.get("localizacao") or ""
        o_que_falta = crit.get("o_que_falta") or ""

        # Validação no código: a citação precisa ser trecho do rascunho
        if evidencia and evidencia != "sem evidência verificável":
            ev_norm = _normalize_text(evidencia)
            if ev_norm and ev_norm not in draft_norm:
                evidencia = "sem evidência verificável"
                logger.info("Citação descartada (não encontrada no rascunho): %s", evidencia[:50])

        lines.append(f"\n<b>{nome}</b>")
        lines.append(f"  Nível: {nivel}")
        if evidencia:
            lines.append(f'  Evidência: "{evidencia}" ({localizacao})')
        else:
            lines.append("  Evidência: sem evidência verificável")
        if o_que_falta:
            lines.append(f"  O que falta: {o_que_falta}")

    # Checagem formal determinística no código
    if formal_checks.get("word_limit"):
        limit = formal_checks["word_limit"]
        unit = formal_checks.get("word_unit", "palavras")
        lines.append("\n<b>Checagem formal</b>")
        if unit == "palavras":
            ok = word_count <= limit
            lines.append(
                f"  Contagem: {word_count} palavras (limite: {limit}) — {'OK' if ok else 'EXCEDEU'}"
            )
        else:
            lines.append(f"  Contagem de páginas não verificada automaticamente (limite: {limit})")

    prioridades = data.get("prioridades") or []
    if prioridades:
        lines.append("\n<b>Prioridades de ajuste:</b>")
        for i, p in enumerate(prioridades[:3], 1):
            lines.append(f"  {i}. {p}")

    aviso = data.get("aviso") or "estimativa automática, não substitui a avaliação do professor"
    if "estimativa automática" not in aviso.lower():
        aviso = "estimativa automática, não substitui a avaliação do professor"
    lines.append(f"\n<i>{aviso}</i>")

    # Verifica que o feedback não contém nota numérica
    feedback_text = "\n".join(lines)
    if re.search(r"\bnota\s*[:=]?\s*\d", feedback_text, re.IGNORECASE):
        logger.warning("Feedback continha nota numérica; removendo")
        feedback_text = re.sub(
            r"\bnota\s*[:=]?\s*\d+\b", "[nota removida]", feedback_text, flags=re.IGNORECASE
        )

    return feedback_text


def save_temp_file(content: bytes, suffix: str, temp_dir: str | None = None) -> Path:
    """Salva conteúdo em arquivo temporário e retorna o caminho."""
    if temp_dir is None:
        temp_dir = tempfile.gettempdir()
    fd, path = tempfile.mkstemp(suffix=suffix, dir=temp_dir)
    try:
        with open(fd, "wb") as f:
            f.write(content)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return Path(path)
