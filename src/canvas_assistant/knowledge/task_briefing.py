import hashlib
import html
import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings, UserProfile
from ..llm.base import LLMProvider, Message
from ..repository import Repository
from ..textutil import short_course_name
from ..timeutil import WEEKDAYS_PT, format_local, local_now, parse_dt
from .report import resolve_course
from .study_context import build_context
from .study_service import StudyResult

logger = logging.getLogger(__name__)

BRIEFING_PROMPT_VERSION = "v2"

_SUBMISSION_TYPE_TRANSLATIONS = {
    "online_upload": "envio de arquivo",
    "online_text_entry": "texto online",
    "online_url": "URL do site",
    "online_quiz": "quiz online",
    "discussion_topic": "fórum de discussão",
    "not_graded": "não avaliada",
    "on_paper": "em papel",
    "external_tool": "ferramenta externa",
    "none": "nenhum",
}

_SUBMISSION_STATE_TRANSLATIONS = {
    "unsubmitted": "não enviada",
    "submitted": "enviada",
    "graded": "corrigida",
    "pending_review": "aguardando correção",
}

_GENERIC_MATERIAL_KEYWORDS = (
    "cronograma",
    "ementa",
    "plano de ensino",
    "guia da disciplina",
    "calendario",
    "calendário",
)


def _translate_submission_types(raw_types: list[str]) -> str:
    if not raw_types:
        return "não especificado"
    translated = [_SUBMISSION_TYPE_TRANSLATIONS.get(t, t) for t in raw_types]
    return ", ".join(translated)


def _cut_at_word_boundary(text: str, max_chars: int = 180) -> str:
    cleaned = re.sub(r"\[(Imagem|Mídia|img):[^\]]*\]", "", text, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[:max_chars].rsplit(" ", 1)[0]
    return f"{cut}…"


_REFUSAL_TERMS = (
    "resolve pra mim",
    "resolve para mim",
    "faca pra mim",
    "faça pra mim",
    "faca para mim",
    "faça para mim",
    "escreva a resposta",
    "escreve a resposta",
    "me da a resposta",
    "me dá a resposta",
    "faz o trabalho",
    "faz a tarefa",
)


def is_resolution_request(text: str) -> bool:
    clean = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return any(term in clean for term in _REFUSAL_TERMS)


def format_refusal_message(task_title: str, topic: str = "") -> str:
    topic_str = f" sobre '{topic}'" if topic else ""
    return (
        "Eu ajudo você a entender e planejar suas tarefas, mas não produzo respostas prontas "
        "nem realizo entregas no seu lugar.\n\n"
        f"• Para entender e planejar esta tarefa, use: <code>/tarefa {task_title}</code>\n"
        f"• Para praticar conceitos com questões de estudo, use: "
        f"<code>/quiz <matéria>{topic_str}</code>\n"
        "(A revisão do seu próprio rascunho de entrega será oferecida em versão futura)."
    )


def is_quiz_task(assignment: dict[str, Any]) -> bool:
    types = json.loads(assignment.get("submission_types_json") or "[]")
    if any(t in ("online_quiz", "external_tool") for t in types):
        return True
    name = (assignment.get("name") or "").upper()
    has_desc = bool((assignment.get("description_html") or "").strip())
    return bool("ATIVIDADE OBJETIVA" in name and not has_desc)


def _days_remaining_label(due_dt: datetime | None, now: datetime) -> tuple[str, bool]:
    if due_dt is None:
        return "sem prazo definido", False
    diff = due_dt - now
    total_seconds = diff.total_seconds()
    due_weekday_time = f"{WEEKDAYS_PT[due_dt.weekday()]} {due_dt:%d/%m %H:%M}"
    is_late = total_seconds < 0
    if is_late:
        past_days = abs(int(total_seconds // 86400))
        if past_days == 0:
            past_hours = max(1, abs(int(total_seconds // 3600)))
            return f"atrasada há {past_hours}h ({due_weekday_time})", True
        return f"atrasada há {past_days}d ({due_weekday_time})", True
    days = int(total_seconds // 86400)
    hours = int((total_seconds % 86400) // 3600)
    if days == 0:
        return f"HOJE, restam {hours}h ({due_weekday_time})", False
    if days == 1:
        return f"amanhã, restam {hours}h ({due_weekday_time})", False
    return f"em {days} dias e {hours} h ({due_weekday_time})", False


def list_pending_tasks_text(
    repo: Repository,
    user_id: str,
    tz: ZoneInfo,
    *,
    course_query: str = "",
) -> str:
    active_courses = repo.get_courses(user_id, current_only=True, include_non_subjects=False)
    if not active_courses:
        return "Nenhuma matéria ativa encontrada no Canvas."

    target_course_ids: list[int] | None = None
    course_filter_name = ""
    if course_query.strip():
        matched, candidates = resolve_course(active_courses, course_query)
        if candidates:
            cand_names = [short_course_name(c.get("name")) for c in candidates]
            return (
                f"Mais de uma matéria corresponde a '{course_query}':\n"
                + "\n".join(f"- {n}" for n in cand_names)
                + "\nEspecifique melhor o nome."
            )
        if not matched:
            return f"Matéria '{course_query}' não encontrada entre as matérias ativas."
        target_course_ids = [int(matched["id"])]
        course_filter_name = short_course_name(matched.get("name"))
    else:
        target_course_ids = [int(c["id"]) for c in active_courses]

    pending = repo.get_pending_assignments(user_id, course_ids=target_course_ids)
    if not pending:
        if course_filter_name:
            return f"Parabéns! Nenhuma tarefa pendente em <b>{course_filter_name}</b>."
        return "Parabéns! Nenhuma tarefa pendente nas matérias ativas."

    now = local_now(tz)

    def _task_sort_key(task: dict[str, Any]) -> tuple[int, datetime]:
        due_iso = task.get("due_at")
        due_dt = parse_dt(due_iso).astimezone(tz) if due_iso else None
        if due_dt is None:
            return (3, datetime.max.replace(tzinfo=tz))
        if due_dt < now:
            return (0, due_dt)
        if (due_dt - now).total_seconds() < 86400 and due_dt.date() == now.date():
            return (1, due_dt)
        return (2, due_dt)

    pending.sort(key=_task_sort_key)

    lines: list[str] = []
    header = (
        f"Tarefas pendentes em <b>{course_filter_name}</b> ({len(pending)}):"
        if course_filter_name
        else f"Tarefas pendentes ({len(pending)}):"
    )
    lines.append(header)
    lines.append("")

    for idx, a in enumerate(pending, 1):
        name = a.get("name") or "Tarefa sem título"
        cname = short_course_name(a.get("course_name"))
        pts = a.get("points_possible")
        pts_str = f"{pts:g} pts" if pts is not None else "sem nota"

        due_iso = a.get("due_at")
        due_dt = parse_dt(due_iso).astimezone(tz) if due_iso else None
        rem_label, is_late = _days_remaining_label(due_dt, now)

        sub_types = json.loads(a.get("submission_types_json") or "[]")
        types_label = _translate_submission_types(sub_types)

        has_desc = bool((a.get("description_html") or "").strip())
        rubric_list = json.loads(a.get("rubric_json") or "[]")
        has_rubric = bool(rubric_list)

        info_flags: list[str] = []
        if has_desc:
            info_flags.append("descrição")
        if has_rubric:
            info_flags.append(f"rubrica ({len(rubric_list)} critérios)")
        flags_str = f" [{', '.join(info_flags)}]" if info_flags else ""

        is_today = bool(
            due_dt
            and not is_late
            and (due_dt - now).total_seconds() < 86400
            and due_dt.date() == now.date()
        )
        if is_late:
            status_tag = "⚠️ <b>[ATRASADA]</b> "
        elif is_today:
            status_tag = "🔥 <b>[HOJE]</b> "
        else:
            status_tag = ""

        lines.append(
            f"<b>{idx}.</b> {status_tag}<b>{name}</b> ({cname})\n"
            f"   Prazo: {rem_label} | {pts_str} | "
            f"Entrega: {types_label}{flags_str}"
        )

    lines.append("")
    lines.append("Use <code>/tarefa &lt;n&gt;</code> para ver o briefing estruturado da tarefa.")
    return "\n".join(lines)


def resolve_task_selection(
    repo: Repository,
    user_id: str,
    query: str,
    tz: ZoneInfo,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    clean = query.strip()
    if not clean:
        return None, [], "Informe o número da tarefa (da lista de /tarefas) ou parte do título."

    active_courses = repo.get_courses(user_id, current_only=True, include_non_subjects=False)
    course_ids = [int(c["id"]) for c in active_courses] if active_courses else None

    if clean.isdigit():
        idx = int(clean)
        pending = repo.get_pending_assignments(user_id, course_ids=course_ids)
        if 1 <= idx <= len(pending):
            target = pending[idx - 1]
            detail = repo.get_assignment_detail(user_id, int(target["id"]))
            return detail or target, [], ""
        return (
            None,
            [],
            f"Número {idx} fora do intervalo. Use /tarefas para ver a lista (1 a {len(pending)}).",
        )

    pending = repo.get_pending_assignments(user_id, course_ids=course_ids)
    q_norm = clean.casefold()
    candidates: list[dict[str, Any]] = []
    for a in pending:
        name = (a.get("name") or "").casefold()
        if q_norm in name:
            candidates.append(a)

    if not candidates:
        all_match = [
            a
            for a in repo.get_pending_assignments(user_id, course_ids=None)
            if q_norm in (a.get("name") or "").casefold()
        ]
        if all_match:
            candidates = all_match

    if not candidates:
        return None, [], f"Nenhuma tarefa pendente encontrada com o termo '{clean}'."
    if len(candidates) == 1:
        target = candidates[0]
        detail = repo.get_assignment_detail(user_id, int(target["id"]))
        return detail or target, [], ""
    return None, candidates, ""


def _build_quiz_briefing_text(assignment: dict[str, Any], tz: ZoneInfo) -> str:
    name = assignment.get("name") or "Quiz"
    cname = short_course_name(assignment.get("course_name"))
    due_iso = assignment.get("due_at")
    due_formatted = format_local(due_iso, tz) if due_iso else "Sem prazo"
    pts = assignment.get("points_possible")
    pts_str = f"{pts:g} pontos" if pts is not None else "Sem pontuação definida"
    raw_state = assignment.get("submission_state") or "unsubmitted"
    state = _SUBMISSION_STATE_TRANSLATIONS.get(raw_state, raw_state)

    return (
        f"<b>📋 Informações do Quiz: {name}</b> ({cname})\n\n"
        f"• <b>Prazo:</b> {due_formatted}\n"
        f"• <b>Pontuação:</b> {pts_str}\n"
        f"• <b>Status:</b> {state}\n\n"
        "ℹ️ <b>Aviso:</b> Esta atividade é um quiz/questionário objetivo do Canvas. "
        "Não geramos briefing de resolução nem respostas para questionários.\n\n"
        f"💡 Para praticar os conceitos desta matéria antes da tentativa, utilize:\n"
        f"<code>/quiz {cname} &lt;tópico&gt;</code>"
    )


def _compute_assignment_content_hash(description_html: str | None, rubric: list[Any]) -> str:
    h = hashlib.sha256()
    h.update((description_html or "").encode("utf-8"))
    h.update(json.dumps(rubric, sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:16]


async def generate_task_briefing(
    repo: Repository,
    provider: LLMProvider,
    user: UserProfile,
    assignment: dict[str, Any],
    tz: ZoneInfo,
    *,
    settings: Settings,
) -> StudyResult:
    if is_quiz_task(assignment):
        text = _build_quiz_briefing_text(assignment, tz)
        return StudyResult(
            text=text,
            input_tokens=0,
            output_tokens=0,
            context_blocks=0,
            latency_ms=0,
            coverage="none",
            from_cache=False,
        )

    assignment_id = int(assignment["id"])
    course_id = int(assignment["course_id"])
    course_name = short_course_name(assignment.get("course_name"))
    task_title = assignment.get("name") or "Tarefa"
    due_iso = assignment.get("due_at")
    due_dt = parse_dt(due_iso).astimezone(tz) if due_iso else None
    rem_label, is_late = _days_remaining_label(due_dt, local_now(tz))
    pts = assignment.get("points_possible")
    pts_str = f"{pts:g} pontos" if pts is not None else "Não informado"
    sub_types = json.loads(assignment.get("submission_types_json") or "[]")
    sub_types_str = _translate_submission_types(sub_types)

    desc_html = assignment.get("description_html") or ""
    clean_desc = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", desc_html)
    clean_desc = re.sub(r"<[^>]+>", " ", clean_desc)
    clean_desc = html.unescape(clean_desc)
    clean_desc = re.sub(r"[ \t]+", " ", clean_desc).strip()

    rubric_list = json.loads(assignment.get("rubric_json") or "[]")

    content_hash = _compute_assignment_content_hash(desc_html, rubric_list)
    cache_key = f"briefing:{assignment_id}:{content_hash}:{BRIEFING_PROMPT_VERSION}"

    cached_text = repo.get_briefing_cache(cache_key)
    if cached_text:
        return StudyResult(
            text=cached_text,
            input_tokens=0,
            output_tokens=0,
            context_blocks=0,
            latency_ms=0,
            coverage="strong",
            from_cache=True,
        )

    # 4. Material do acervo relacionado na MESMA matéria
    search_query = f"{task_title} {clean_desc[:200]}"
    context_pack = build_context(
        repo,
        user.user_id,
        search_query,
        course_id=course_id,
        source_types=("arquivo", "pagina", "ementa"),
        k=settings.study.top_k,
        max_tokens=settings.study.context_max_tokens,
    )

    task_title_folded = task_title.casefold()
    generic_in_title = any(kw in task_title_folded for kw in _GENERIC_MATERIAL_KEYWORDS)

    related_materials_lines: list[str] = []
    filtered_blocks = []
    if context_pack.blocks:
        for b in context_pack.blocks:
            fname_folded = b.filename.casefold()
            loc_folded = b.location_label.casefold()
            if not generic_in_title and any(
                kw in fname_folded or kw in loc_folded for kw in _GENERIC_MATERIAL_KEYWORDS
            ):
                continue
            filtered_blocks.append(b)
            if len(filtered_blocks) >= 3:
                break

    if filtered_blocks:
        for b in filtered_blocks:
            loc = b.location_label
            fname = b.filename
            snippet = _cut_at_word_boundary(b.text, max_chars=180)
            related_materials_lines.append(f'• <b>{fname}</b> ({loc}):\n  <i>"{snippet}"</i>')
    else:
        related_materials_lines.append(
            "• Nenhum arquivo ou página diretamente relacionado no acervo da matéria."
        )

    rubric_checklist_lines: list[str] = []
    total_rubric_pts = 0.0
    has_rubric_points = False
    if rubric_list:
        for item in rubric_list:
            crit = item.get("criterion") or item.get("description") or "Critério"
            p = item.get("points")
            if p is not None:
                p_float = float(p)
                total_rubric_pts += p_float
                has_rubric_points = True
                p_label = f"{p_float:g} pts"
            else:
                p_label = ""
            desc = item.get("description") or ""
            desc_sub = f" — {desc[:120]}" if desc and desc != crit else ""
            rubric_checklist_lines.append(f"• [ ] <b>{crit}</b> ({p_label}){desc_sub}")

        if (
            has_rubric_points
            and pts is not None
            and abs(total_rubric_pts - float(pts)) > 0.01
            and total_rubric_pts > 0
        ):
            rubric_checklist_lines.append(
                f"\n⚠️ <i>Atenção: A soma dos critérios da rubrica ({total_rubric_pts:g} pts) "
                f"diverge da pontuação total da tarefa ({pts:g} pts).</i>"
            )
    else:
        rubric_checklist_lines.append(
            "• Nenhuma rubrica formal foi cadastrada no Canvas para esta tarefa."
        )

    system_prompt = (
        "Você é um tutor acadêmico do assistente PUC Minas.\n"
        "Sua função é gerar um BRIEFING ESTRUTURADO para ajudar o aluno a ENTENDER e PLANEJAR.\n"
        "REGRAS INVIOLÁVEIS:\n"
        "1. NUNCA gere o entregável, NUNCA responda às questões e NUNCA redija a submissão.\n"
        "2. Seção '1. O que pedem': resumo fiel do enunciado. NÃO acrescente exigências.\n"
        "3. Seção '5. Plano de trabalho sugerido': passos ordenados com tempo (SUGESTÃO).\n"
        "4. Seção '6. Perguntas para se guiar': 3 a 5 perguntas reflexivas para o aluno.\n"
        "Responda estritamente nas seções 1, 5 e 6 no formato abaixo."
    )

    user_prompt = (
        f"Matéria: {course_name}\n"
        f"Título da Tarefa: {task_title}\n"
        f"Enunciado da Tarefa:\n{clean_desc or '(Enunciado sem texto na descrição da tarefa)'}\n\n"
        "Gere APENAS o conteúdo das seções 1, 5 e 6:\n"
        "1. O que pedem:\n"
        "<resumo fiel do enunciado>\n\n"
        "5. Plano de trabalho sugerido (Sugestão):\n"
        "<passos com estimativa de tempo>\n\n"
        "6. Perguntas para se guiar:\n"
        "<3 a 5 perguntas reflexivas>"
    )

    t0 = time.perf_counter()
    briefing_messages = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_prompt),
    ]
    response = await provider.chat(briefing_messages, tools=[])
    latency_ms = int((time.perf_counter() - t0) * 1000)

    prompt_chars = sum(len(m.content) for m in briefing_messages)
    est_in_tok = max(1, prompt_chars // 4)
    prompt_eval_count = int(getattr(response, "input_tokens", 0) or 0)
    logger.info(
        "Briefing LLM: %sms | prompt_chars=%s est_in_tok=%s prompt_eval_count=%s "
        "in_tok=%s out_tok=%s blocos=%s",
        latency_ms,
        prompt_chars,
        est_in_tok,
        prompt_eval_count,
        response.input_tokens,
        response.output_tokens,
        len(context_pack.blocks),
    )

    llm_output = response.text or ""

    sec1_match = re.search(r"(?is)1\.\s*O que pedem.*?:?\s*(.*?)(?=\n\s*5\.|\Z)", llm_output)
    sec5_match = re.search(r"(?is)5\.\s*Plano de trabalho.*?:?\s*(.*?)(?=\n\s*6\.|\Z)", llm_output)
    sec6_match = re.search(r"(?is)6\.\s*Perguntas para se guiar.*?:?\s*(.*?)\Z", llm_output)

    sec1_text = sec1_match.group(1).strip() if sec1_match else clean_desc[:400]
    sec5_text = (
        sec5_match.group(1).strip()
        if sec5_match
        else (
            "• Passo 1: Leitura do material base (1h)\n"
            "• Passo 2: Elaboração do rascunho inicial (2h)\n"
            "• Passo 3: Revisão contra a rubrica e formatação (1h)"
        )
    )
    sec6_text = (
        sec6_match.group(1).strip()
        if sec6_match
        else (
            "• O documento atende a todos os requisitos do enunciado?\n"
            "• A entrega respeita a data limite e o formato solicitado?\n"
            "• Cada item da rubrica foi contemplado no material produzido?"
        )
    )

    late_badge = " ⚠️ <b>[ATRASADA]</b>" if is_late else ""
    briefing_parts = [
        f"<b>📋 Briefing da Tarefa: {task_title}</b> ({course_name})\n",
        f"<b>1. O que pedem</b>\n{sec1_text}\n",
        f"<b>2. Prazo e entrega</b>\n"
        f"• Prazo: {rem_label}{late_badge}\n"
        f"• Pontos: {pts_str}\n"
        f"• Tipos de entrega: {sub_types_str}\n",
        "<b>3. Checklist da rubrica</b>\n" + "\n".join(rubric_checklist_lines) + "\n",
        "<b>4. Material do acervo relacionado</b>\n" + "\n".join(related_materials_lines) + "\n",
        f"<b>5. Plano de trabalho sugerido (sugestão)</b>\n{sec5_text}\n",
        f"<b>6. Perguntas para se guiar</b>\n{sec6_text}\n",
        "<i>ℹ️ Este briefing ajuda a planejar sua tarefa. "
        "O bot não produz respostas entregáveis nem redige trabalhos.</i>",
    ]

    final_text = "\n".join(briefing_parts)

    repo.save_briefing_cache(
        cache_key,
        user_id=user.user_id,
        assignment_id=assignment_id,
        content_hash=content_hash,
        prompt_version=BRIEFING_PROMPT_VERSION,
        briefing_text=final_text,
    )

    repo.record_study_usage(
        user.user_id,
        "tarefa",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        context_blocks=len(context_pack.blocks),
        latency_ms=latency_ms,
    )

    return StudyResult(
        text=final_text,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        context_blocks=len(context_pack.blocks),
        latency_ms=latency_ms,
        coverage=context_pack.coverage,
        from_cache=False,
    )
