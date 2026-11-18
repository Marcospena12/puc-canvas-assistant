from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..config import Settings, UserProfile
from ..llm.base import LLMProvider, Message
from ..repository import Repository
from ..textutil import short_course_name
from .report import resolve_course
from .search import search as fts_search
from .study_context import (
    DEFAULT_STUDY_CONTEXT_MAX_TOKENS,
    DEFAULT_STUDY_GROUNDING_MIN_OVERLAP,
    DEFAULT_STUDY_SUMMARY_MAX_CHUNKS,
    DEFAULT_STUDY_TOP_K,
    StudyContextPack,
    build_context,
    build_study_system_prompt,
    calculate_grounding_overlap,
    extract_content_stems,
    post_process_study_answer,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION_EXPLAIN = "v1"
PROMPT_VERSION_SUMMARY = "v1"
PROMPT_VERSION_QUIZ = "v1"

_LAST_QUIZ_CACHE: dict[str, tuple[float, QuizData]] = {}
QUIZ_CACHE_TTL_SECONDS = 3600 * 4  # 4 horas de expiração


@dataclass(frozen=True, slots=True)
class StudyResult:
    text: str
    input_tokens: int
    output_tokens: int
    context_blocks: int
    latency_ms: int
    coverage: str
    from_cache: bool = False


class QuizQuestion(BaseModel):
    id: int
    type: str = Field(description="'multiple_choice' ou 'discursive'")
    question: str
    options: list[str] = Field(
        default_factory=list,
        description="Lista de alternativas A), B), C), D) se for múltipla escolha",
    )
    correct_answer: str
    explanation: str
    source: str


class QuizData(BaseModel):
    topic: str
    course: str
    questions: list[QuizQuestion]


def _clean_markdown_json(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


async def _call_llm_with_retry(
    provider: LLMProvider,
    messages: list[Message],
    *,
    max_retries: int = 2,
    base_delay: float = 1.0,
) -> tuple[str, int, int, int]:
    """Chama o provider LLM com retry curto e mede latência e tokens."""
    last_exc: Exception | None = None
    t0 = time.perf_counter()

    for attempt in range(max_retries + 1):
        try:
            resp = await provider.chat(messages, tools=[])
            latency_ms = int((time.perf_counter() - t0) * 1000)
            in_tok = resp.input_tokens
            out_tok = resp.output_tokens
            # Se o provider nǜo reportou tokens (ex.: mock), estima por caracteres
            prompt_chars = sum(len(m.content) for m in messages)
            est_in_tok = max(1, prompt_chars // 4)
            if in_tok == 0:
                in_tok = est_in_tok
            elif in_tok < est_in_tok * 0.70 and est_in_tok > 1000:
                logger.warning(
                    "Poss��vel truncamento no LLM: tokens in (%s) << estimado (%s)",
                    in_tok,
                    est_in_tok,
                )
            if out_tok == 0:
                out_tok = max(1, len(resp.text) // 4)
            logger.info(
                "LLM: %sms | load=%sms prompt_eval=%sms eval=%sms | "
                "prompt_chars=%s est_in_tok=%s in_tok=%s out_tok=%s thinking_chars=%s",
                latency_ms,
                resp.load_duration_ms,
                resp.prompt_eval_duration_ms,
                resp.eval_duration_ms,
                prompt_chars,
                est_in_tok,
                in_tok,
                out_tok,
                resp.thinking_chars,
            )
            return resp.text, in_tok, out_tok, latency_ms
        except Exception as exc:
            last_exc = exc
            logger.warning("Tentativa %s/%s no LLM falhou: %s", attempt + 1, max_retries + 1, exc)
            if attempt < max_retries:
                await asyncio.sleep(base_delay * (attempt + 1))

    raise RuntimeError(f"Falha na comunicação com o modelo de linguagem: {last_exc}") from last_exc


def parse_topic_and_course(raw_text: str) -> tuple[str, str | None]:
    """Extrai matéria e tópico de padrões como 'BI: modelagem' ou 'DAM - React Native'."""
    text = (raw_text or "").strip()
    if not text:
        return "", None

    if ":" in text:
        part_c, part_t = text.split(":", 1)
        if part_t.strip():
            return part_t.strip(), part_c.strip()

    return text, None


def resolve_course_or_candidates(
    repo: Repository, user_id: str, course_query: str | None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """Resolve curso de forma tolerante com desambiguação amigável."""
    if not course_query:
        return None, [], None

    courses = repo.get_courses(user_id, current_only=True, include_non_subjects=False)
    if not courses:
        courses = repo.get_courses(user_id, current_only=False, include_non_subjects=False)

    matched, candidates = resolve_course(courses, course_query)
    if candidates:
        names = [f"- {short_course_name(c.get('name'))}" for c in candidates]
        err_msg = f"Mais de uma matéria corresponde a '{course_query}':\n" + "\n".join(names)
        return None, candidates, err_msg

    if not matched:
        return (
            None,
            [],
            f"Matéria '{course_query}' não encontrada. Tente buscar pelo nome completo ou código.",
        )

    return matched, [], None


def resolve_file_or_candidates(
    repo: Repository, user_id: str, course_id: int | None, file_query: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """Resolve arquivo de aula por nome ou código, com desambiguação."""
    clean = file_query.strip().lower()
    files = repo.get_course_files(user_id, course_id=course_id)
    if not files and course_id is not None:
        files = repo.get_course_files(user_id, course_id=None)

    matches: list[dict[str, Any]] = []
    for f in files:
        display = (f.get("display_name") or "").lower()
        if clean in display:
            matches.append(f)

    # Se não encontrou arquivo, busca páginas, tarefas e avisos em canvas_content_state
    if not matches:
        content_items = repo.list_all_content_items(user_id, course_id=course_id)
        for item in content_items:
            t_lower = (item.get("title") or "").lower()
            if clean in t_lower:
                matches.append(
                    {
                        "file_id": 0,
                        "display_name": item["title"],
                        "sha256": item["content_hash"],
                        "source_type": item["source_type"],
                        "source_ref": item["source_ref"],
                        "course_id": item["course_id"],
                        "is_content": True,
                    }
                )

    if not matches:
        return None, [], f"Nenhum arquivo ou conteúdo encontrado com o termo '{file_query}'."

    if len(matches) > 1:
        # Se um deles for casamento exato (sem extensão), prioriza
        exact = [f for f in matches if (f.get("display_name") or "").lower().split(".")[0] == clean]
        if len(exact) == 1:
            return exact[0], [], None

        cands = [f"- {f.get('display_name')}" for f in matches[:5]]
        err_msg = (
            f"Mais de um arquivo corresponde a '{file_query}':\n"
            + "\n".join(cands)
            + "\nEspecifique o nome do arquivo com mais precisão."
        )
        return None, matches, err_msg

    return matches[0], [], None


def parse_location_range(raw_range: str | None) -> tuple[int | None, int | None, str]:
    if not raw_range or not raw_range.strip():
        return None, None, ""

    clean = raw_range.strip().lower()
    # Procura padrões de intervalo como '10-30', 'páginas 10 a 30', 'slides 1-15'
    nums = [int(n) for n in re.findall(r"\d+", clean)]
    if len(nums) >= 2:
        return nums[0], nums[1], f"{nums[0]}-{nums[1]}"
    if len(nums) == 1:
        return nums[0], nums[0], f"{nums[0]}"
    return None, None, clean


def _find_videoaula_for_topic(
    repo: Repository,
    user_id: str,
    topic: str,
    course_id: int | None,
) -> list[str]:
    """Busca chunks com marcador [Vídeo:] que casam com o tópico e retorna títulos de páginas."""
    results = fts_search(
        repo,
        user_id,
        topic,
        course_id=course_id,
        source_types=("pagina",),
        k=5,
    )
    pages: list[str] = []
    seen: set[str] = set()
    for item in results.items:
        text = getattr(item, "text", "") or ""
        title = getattr(item, "title", "") or ""
        if "[Vídeo:" in text and title and title not in seen:
            seen.add(title)
            pages.append(title)
    return pages


async def explain_topic(
    repo: Repository,
    provider: LLMProvider,
    user: UserProfile,
    topic_query: str,
    *,
    course_query: str | None = None,
    settings: Settings | None = None,
) -> StudyResult:
    """Gera uma explicação fundamentada de até ~12 linhas sobre o tópico."""
    extracted_topic, inline_course = parse_topic_and_course(topic_query)
    c_query = course_query or inline_course

    target_course_id: int | None = None
    if c_query:
        matched_c, _, err_c = resolve_course_or_candidates(repo, user.user_id, c_query)
        if err_c:
            return StudyResult(
                text=err_c,
                input_tokens=0,
                output_tokens=0,
                context_blocks=0,
                latency_ms=0,
                coverage="none",
            )
        if matched_c:
            target_course_id = int(matched_c["id"])

    top_k = settings.study.top_k if settings else DEFAULT_STUDY_TOP_K
    max_tokens = settings.study.context_max_tokens if settings else DEFAULT_STUDY_CONTEXT_MAX_TOKENS
    min_overlap = (
        settings.study.grounding_min_overlap if settings else DEFAULT_STUDY_GROUNDING_MIN_OVERLAP
    )

    pack: StudyContextPack = build_context(
        repo,
        user.user_id,
        extracted_topic,
        course_id=target_course_id,
        k=top_k,
        max_tokens=max_tokens,
    )

    sys_prompt = build_study_system_prompt(pack.coverage)
    user_prompt = (
        f"Pergunta do aluno: {extracted_topic}\n\n"
        f"MATERIAL DE AULA RECUPERADO:\n"
        f"{pack.context_text if pack.context_text else '(Nenhum material encontrado no cache)'}\n\n"
        "Explique o tópico de forma clara, direta e didática em até 12 linhas. "
        "Cite a fonte de cada ponto afirmado usando o identificador [Fn]."
    )

    messages = [
        Message(role="system", content=sys_prompt),
        Message(role="user", content=user_prompt),
    ]

    try:
        raw_answer, in_tok, out_tok, lat_ms = await _call_llm_with_retry(provider, messages)
    except Exception as exc:
        return StudyResult(
            text=f"⚠️ Não foi possível gerar a explicação no momento: {exc}",
            input_tokens=0,
            output_tokens=0,
            context_blocks=len(pack.blocks),
            latency_ms=0,
            coverage=pack.coverage,
        )

    processed_text = post_process_study_answer(
        raw_answer,
        pack.sources_map,
        min_overlap=min_overlap,
        coverage=pack.coverage,
        context_texts=[block.text for block in pack.blocks],
    )

    # Aviso de videoaula: se coverage for weak/none mas há chunk com [Vídeo:] que casa
    if pack.coverage in ("weak", "none"):
        video_pages = _find_videoaula_for_topic(
            repo, user.user_id, extracted_topic, target_course_id
        )
        if video_pages:
            video_note = (
                " ℹ️ Há uma videoaula sobre isso em "
                + ", ".join(video_pages[:3])
                + ", que não consigo ler."
            )
            processed_text = processed_text + video_note

    # Registra observabilidade
    repo.record_study_usage(
        user.user_id,
        "explain",
        input_tokens=in_tok,
        output_tokens=out_tok,
        context_blocks=len(pack.blocks),
        latency_ms=lat_ms,
    )
    logger.info(
        "Study /explicar: %s blocos, %s in_tok, %s out_tok, %sms latência",
        len(pack.blocks),
        in_tok,
        out_tok,
        lat_ms,
    )

    return StudyResult(
        text=processed_text,
        input_tokens=in_tok,
        output_tokens=out_tok,
        context_blocks=len(pack.blocks),
        latency_ms=lat_ms,
        coverage=pack.coverage,
    )


async def summarize_file(
    repo: Repository,
    provider: LLMProvider,
    user: UserProfile,
    course_query: str,
    file_query: str,
    location_range_raw: str | None = None,
    *,
    settings: Settings | None = None,
) -> StudyResult:
    """Gera resumo estruturado com cache sha256 e exigência de faixa para arquivos longos."""
    matched_c, _, err_c = resolve_course_or_candidates(repo, user.user_id, course_query)
    if err_c:
        return StudyResult(
            text=err_c,
            input_tokens=0,
            output_tokens=0,
            context_blocks=0,
            latency_ms=0,
            coverage="none",
        )

    target_course_id = int(matched_c["id"]) if matched_c else None
    matched_f, _, err_f = resolve_file_or_candidates(
        repo, user.user_id, target_course_id, file_query
    )
    if err_f:
        return StudyResult(
            text=err_f,
            input_tokens=0,
            output_tokens=0,
            context_blocks=0,
            latency_ms=0,
            coverage="none",
        )

    assert matched_f is not None
    file_id = int(matched_f["file_id"])
    file_sha = str(matched_f.get("sha256") or f"file_{file_id}")
    file_name = str(matched_f.get("display_name") or f"Arquivo_{file_id}")

    if matched_f.get("is_content"):
        c_id = target_course_id or int(matched_f.get("course_id", 0))
        chunks = repo.get_chunks_by_content(
            user.user_id, c_id, matched_f["source_type"], matched_f["source_ref"]
        )
    else:
        chunks = repo.get_chunks_by_file(user.user_id, file_id)

    if not chunks:
        return StudyResult(
            text=f"Não há texto extraído para '{file_name}'. Execute /sync ou 'extract'.",
            input_tokens=0,
            output_tokens=0,
            context_blocks=0,
            latency_ms=0,
            coverage="none",
        )

    max_chunks_limit = (
        settings.study.summary_max_chunks if settings else DEFAULT_STUDY_SUMMARY_MAX_CHUNKS
    )

    start_num, end_num, range_label = parse_location_range(location_range_raw)

    # Exigência de faixa para arquivos longos sem faixa especificada
    if len(chunks) > max_chunks_limit and not range_label:
        unit_type = chunks[0].get("location_type", "página")
        total_units = len(chunks)
        c_hint = short_course_name(matched_c["name"]) if matched_c else course_query
        return StudyResult(
            text=(
                f"O arquivo <b>{file_name}</b> é extenso ({total_units} {unit_type}s).\n\n"
                f"Para manter alta qualidade no resumo e controlar custos de processamento, "
                f"por favor informe a faixa desejada.\n"
                f"Exemplo: <code>/resumo {c_hint} {file_query} {unit_type}s 1-20</code>"
            ),
            input_tokens=0,
            output_tokens=0,
            context_blocks=len(chunks),
            latency_ms=0,
            coverage="none",
        )

    # Filtra chunks se houver faixa especificada
    selected_chunks = chunks
    if start_num is not None or end_num is not None:
        s_val = start_num or 1
        e_val = end_num or 999999
        selected_chunks = [
            c
            for c in chunks
            if not (c.get("location_end", 0) < s_val or c.get("location_start", 0) > e_val)
        ]
        if not selected_chunks:
            return StudyResult(
                text=(f"Nenhum conteúdo encontrado na faixa ({range_label}) para '{file_name}'."),
                input_tokens=0,
                output_tokens=0,
                context_blocks=0,
                latency_ms=0,
                coverage="none",
            )

    # Verifica cache existente
    cache_key = hashlib.sha256(
        f"{file_sha}:{range_label or 'all'}:{PROMPT_VERSION_SUMMARY}".encode()
    ).hexdigest()

    cached_text = repo.get_study_cache(cache_key)
    if cached_text:
        logger.info("Study /resumo: cache hit para %s (%s)", file_name, range_label)
        return StudyResult(
            text=f"<i>[Resumo em cache]</i>\n\n{cached_text}",
            input_tokens=0,
            output_tokens=0,
            context_blocks=len(selected_chunks),
            latency_ms=0,
            coverage="strong",
            from_cache=True,
        )

    # Monta texto dos chunks respeitando orçamento
    pieces = []
    for c in selected_chunks:
        loc = c.get("location_label", "")
        pieces.append(f"[{loc}]\n{c.get('text', '')}")
    doc_text = "\n\n".join(pieces)

    sys_prompt = (
        "Você é um tutor acadêmico especialista em sintetizar materiais didáticos.\n"
        "Gere um resumo estruturado, focado e objetivo, contendo:\n"
        "1. **Ideias Principais**: os pontos centrais e teses do material.\n"
        "2. **Termos-chave e Conceitos**: definições fundamentais abordadas.\n"
        "3. **Pontos para focar na revisão**: tópicos com alta probabilidade em avaliações.\n"
        "Seja direto e conciso, evitando prolixidade. Não invente nada fora do texto fornecido."
    )
    user_prompt = (
        f"Material para resumo: {file_name} "
        f"({'faixa: ' + range_label if range_label else 'conteúdo completo'})\n\n"
        f"{doc_text}"
    )

    messages = [
        Message(role="system", content=sys_prompt),
        Message(role="user", content=user_prompt),
    ]

    try:
        summary_raw, in_tok, out_tok, lat_ms = await _call_llm_with_retry(provider, messages)
    except Exception as exc:
        return StudyResult(
            text=f"⚠️ Não foi possível gerar o resumo no momento: {exc}",
            input_tokens=0,
            output_tokens=0,
            context_blocks=len(selected_chunks),
            latency_ms=0,
            coverage="strong",
        )

    loc_summary = range_label if range_label else f"{len(selected_chunks)} trechos"
    final_text = (
        f"<b>Resumo: {file_name}</b> ({loc_summary})\n\n"
        f"{summary_raw.strip()}\n\n"
        f"<b>Fontes:</b>\n- {file_name} ({loc_summary})"
    )

    # Salva no cache
    repo.save_study_cache(
        cache_key,
        file_sha256=file_sha,
        location_range=range_label or "all",
        prompt_version=PROMPT_VERSION_SUMMARY,
        summary_text=final_text,
    )

    # Registra métrica de observabilidade
    repo.record_study_usage(
        user.user_id,
        "summary",
        input_tokens=in_tok,
        output_tokens=out_tok,
        context_blocks=len(selected_chunks),
        latency_ms=lat_ms,
    )
    logger.info(
        "Study /resumo: gerado e cacheado para %s (%s blocos, %s in_tok, %s out_tok, %sms)",
        file_name,
        len(selected_chunks),
        in_tok,
        out_tok,
        lat_ms,
    )

    return StudyResult(
        text=final_text,
        input_tokens=in_tok,
        output_tokens=out_tok,
        context_blocks=len(selected_chunks),
        latency_ms=lat_ms,
        coverage="strong",
    )


def _find_matching_chunk_text(source: str, pack: StudyContextPack) -> tuple[str, str]:
    """Retorna (chunk_text, readable_label) para a fonte citada."""
    match = re.search(r"\[F(\d+)\]", source)
    if match:
        key = f"[F{match.group(1)}]"
        if key in pack.sources_map:
            c_info = pack.sources_map[key]
            text = c_info[3] if len(c_info) > 3 else ""
            label = f"{c_info[1]} ({c_info[2]})"
            return text, label
    s_lower = source.lower()
    for _key, c_info in pack.sources_map.items():
        if c_info[1].lower() in s_lower or s_lower in c_info[1].lower():
            text = c_info[3] if len(c_info) > 3 else ""
            label = f"{c_info[1]} ({c_info[2]})"
            return text, label
    if pack.blocks:
        b0 = pack.blocks[0]
        return b0.text, f"{b0.filename} ({b0.location_label})"
    return "", source


def _jaccard_similarity(stems_a: set[str], stems_b: set[str]) -> float:
    union = stems_a | stems_b
    if not union:
        return 0.0
    return len(stems_a & stems_b) / len(union)


async def generate_quiz(
    repo: Repository,
    provider: LLMProvider,
    user: UserProfile,
    course_query: str,
    topic_query: str,
    n: int = 5,
    *,
    settings: Settings | None = None,
) -> tuple[QuizData | None, StudyResult]:
    """Gera quiz de N questões com base estrita no material recuperado."""
    num_questions = max(1, min(10, n))

    matched_c, _, err_c = resolve_course_or_candidates(repo, user.user_id, course_query)
    if err_c:
        return None, StudyResult(
            text=err_c,
            input_tokens=0,
            output_tokens=0,
            context_blocks=0,
            latency_ms=0,
            coverage="none",
        )

    target_course_id = int(matched_c["id"]) if matched_c else None
    c_name = short_course_name(matched_c["name"]) if matched_c else course_query

    pack: StudyContextPack = build_context(
        repo,
        user.user_id,
        topic_query,
        course_id=target_course_id,
        k=settings.study.top_k if settings else DEFAULT_STUDY_TOP_K,
        max_tokens=settings.study.context_max_tokens
        if settings
        else DEFAULT_STUDY_CONTEXT_MAX_TOKENS,
    )

    if not pack.blocks or pack.coverage == "none":
        return None, StudyResult(
            text=(
                f"Não encontrei material suficiente sobre '{topic_query}' "
                "para formular um quiz com base nas aulas."
            ),
            input_tokens=0,
            output_tokens=0,
            context_blocks=0,
            latency_ms=0,
            coverage="none",
        )

    sys_prompt = (
        "Você é um elaborador de avaliações acadêmicas formativas para estudo.\n"
        "Crie questões baseadas EXCLUSIVAMENTE nos blocos de conteúdo fornecidos.\n"
        "IMPORTANTE: Ferramenta de estudo; NUNCA resolva nem envie quizzes do Canvas.\n\n"
        "A resposta DEVE ser rigorosamente um JSON válido (sem texto antes/depois):\n"
        "{\n"
        f'  "topic": "{topic_query}",\n'
        f'  "course": "{c_name}",\n'
        '  "questions": [\n'
        "    {\n"
        '      "id": 1,\n'
        '      "type": "multiple_choice",\n'
        '      "question": "Enunciado da questão",\n'
        '      "options": ["A) Opção 1", "B) Opção 2", "C) Opção 3", "D) Opção 4"],\n'
        '      "correct_answer": "A) Opção 1",\n'
        '      "explanation": "Explicação detalhada da resposta",\n'
        '      "source": "Arquivo.pdf (Slide ou Página X)"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Elabore misto de múltipla escolha e discursivas curtas (discursivas: options vazio)."
    )

    user_prompt = (
        f"Gere exatamente {num_questions} questões sobre '{topic_query}'.\n\n"
        f"MATERIAL DIDÁTICO BASE:\n{pack.context_text}"
    )

    messages = [
        Message(role="system", content=sys_prompt),
        Message(role="user", content=user_prompt),
    ]

    quiz_obj: QuizData | None = None
    total_in_tok = 0
    total_out_tok = 0
    total_lat_ms = 0

    # Tentativa 1 e retry se JSON vier inválido
    for attempt in range(2):
        try:
            raw_json, in_tok, out_tok, lat_ms = await _call_llm_with_retry(provider, messages)
            total_in_tok += in_tok
            total_out_tok += out_tok
            total_lat_ms += lat_ms

            cleaned = _clean_markdown_json(raw_json)
            parsed_dict = json.loads(cleaned)
            quiz_obj = QuizData.model_validate(parsed_dict)
            break
        except (json.JSONDecodeError, ValidationError) as parse_exc:
            logger.warning(
                "Falha ao validar JSON do quiz (tentativa %s): %s", attempt + 1, parse_exc
            )
            if attempt == 0:
                messages.append(
                    Message(
                        role="user",
                        content="JSON inválido. Retorne APENAS o JSON válido conforme o esquema.",
                    )
                )

    if quiz_obj is None:
        return None, StudyResult(
            text="Dificuldade temporária para estruturar as questões do quiz. Tente novamente.",
            input_tokens=total_in_tok,
            output_tokens=total_out_tok,
            context_blocks=len(pack.blocks),
            latency_ms=total_lat_ms,
            coverage=pack.coverage,
        )

    # Validação de fundamentação (A1), fallback de explanation (A2) e deduplicação (A6)
    min_overlap = (
        settings.study.grounding_min_overlap if settings else DEFAULT_STUDY_GROUNDING_MIN_OVERLAP
    )

    validated_questions: list[QuizQuestion] = []
    seen_answer_stems: list[set[str]] = []

    for q in quiz_obj.questions:
        chunk_text, readable_source = _find_matching_chunk_text(q.source, pack)

        # Fallback de explanation vazio (A2): extrair snippet literal do chunk ou omitir
        explanation = (q.explanation or "").strip()
        if not explanation and chunk_text:
            raw_snip = " ".join(chunk_text.split())
            explanation = raw_snip[:180].rstrip() + ("..." if len(raw_snip) > 180 else "")

        # Verificação de fundamentação (A1): overlap com chunk citado
        check_text = f"{q.correct_answer} {explanation}"
        overlap = calculate_grounding_overlap(check_text, chunk_text) if chunk_text else 1.0

        if overlap < min_overlap and chunk_text:
            logger.warning(
                "Questão %s descartada por baixa fundamentação (overlap %.2f < %.2f)",
                q.id,
                overlap,
                min_overlap,
            )
            continue

        # Verificação de redundância (A6)
        ans_stems = extract_content_stems(q.correct_answer)
        if any(_jaccard_similarity(ans_stems, prev) > 0.60 for prev in seen_answer_stems):
            logger.warning("Questão %s descartada por redundância de resposta", q.id)
            continue

        seen_answer_stems.append(ans_stems)
        validated_questions.append(
            QuizQuestion(
                id=len(validated_questions) + 1,
                type=q.type,
                question=q.question,
                options=q.options,
                correct_answer=q.correct_answer,
                explanation=explanation,
                source=readable_source,
            )
        )

    if not validated_questions:
        return None, StudyResult(
            text="Não foi possível validar questões com fundamentação suficiente. Tente novamente.",
            input_tokens=total_in_tok,
            output_tokens=total_out_tok,
            context_blocks=len(pack.blocks),
            latency_ms=total_lat_ms,
            coverage=pack.coverage,
        )

    quiz_obj = QuizData(
        topic=quiz_obj.topic,
        course=quiz_obj.course,
        questions=validated_questions,
    )

    # Armazena quiz em cache para consulta de /gabarito
    _LAST_QUIZ_CACHE[user.user_id] = (time.time(), quiz_obj)

    # Registra uso de métrica
    repo.record_study_usage(
        user.user_id,
        "quiz",
        input_tokens=total_in_tok,
        output_tokens=total_out_tok,
        context_blocks=len(pack.blocks),
        latency_ms=total_lat_ms,
    )

    # Formata apresentação das perguntas (sem respostas)
    lines = [
        f"📝 <b>Quiz de Estudo: {quiz_obj.topic}</b> ({quiz_obj.course})",
        f"<i>Questões: {len(quiz_obj.questions)} | Consulte o gabarito com /gabarito</i>\n",
    ]
    for q in quiz_obj.questions:
        q_type_str = "Múltipla Escolha" if q.type == "multiple_choice" else "Discursiva"
        lines.append(f"<b>Questão {q.id}</b> ({q_type_str}):")
        lines.append(f"{q.question}")
        if q.options:
            for opt in q.options:
                lines.append(f"  {opt}")
        lines.append("")

    lines.append("Envie <b>/gabarito</b> para conferir as respostas comentadas e as fontes.")
    presentation = "\n".join(lines)

    return quiz_obj, StudyResult(
        text=presentation,
        input_tokens=total_in_tok,
        output_tokens=total_out_tok,
        context_blocks=len(pack.blocks),
        latency_ms=total_lat_ms,
        coverage=pack.coverage,
    )


def get_quiz_answer_key(user_id: str) -> str:
    """Devolve o gabarito do último quiz gerado pelo usuário."""
    cached = _LAST_QUIZ_CACHE.get(user_id)
    if not cached:
        return "Nenhum quiz recente encontrado. Gere um novo com /quiz <matéria> <tópico>."

    ts, quiz_obj = cached
    if time.time() - ts > QUIZ_CACHE_TTL_SECONDS:
        return "O quiz anterior expirou. Gere um novo com /quiz <matéria> <tópico>."

    lines = [
        f"✅ <b>Gabarito e Comentários: {quiz_obj.topic}</b> ({quiz_obj.course})\n",
    ]
    for q in quiz_obj.questions:
        lines.append(f"<b>Questão {q.id}:</b> {q.question}")
        lines.append(f"<b>Resposta correta:</b> {q.correct_answer}")
        if q.explanation:
            lines.append(f"<b>Explicação:</b> {q.explanation}")
        lines.append(f"<i>Fonte: {q.source}</i>\n")

    return "\n".join(lines).strip()


def get_usage_report(repo: Repository, user_id: str, days: int = 7) -> str:
    """Relatório de consumo de tokens e latência dos comandos de estudo."""
    summary = repo.get_study_usage_summary(user_id, days=days)
    if not summary:
        return f"Nenhum comando de estudo executado nos últimos {days} dias."

    lines = [f"📊 <b>Uso de Recursos de Estudo (Últimos {days} dias):</b>\n"]
    total_in = 0
    total_out = 0
    total_execs = 0

    for row in summary:
        cmd = row["command"]
        execs = row["executions"]
        in_tok = row["total_input_tokens"] or 0
        out_tok = row["total_output_tokens"] or 0
        avg_lat = int(row["avg_latency_ms"] or 0)
        total_in += in_tok
        total_out += out_tok
        total_execs += execs
        lines.append(
            f"• <b>/{cmd}</b>: {execs} execuções | "
            f"{in_tok:,} in / {out_tok:,} out tokens | méd. {avg_lat}ms"
        )

    lines.append(
        f"\n<b>Total:</b> {total_execs} execuções | {total_in + total_out:,} tokens "
        f"({total_in:,} entrada, {total_out:,} saída)"
    )
    return "\n".join(lines)
