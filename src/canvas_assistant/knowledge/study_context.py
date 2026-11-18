from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..repository import Repository
from ..textutil import short_course_name
from ..urls import extract_urls, normalize_url
from .search import PORTUGUESE_STOPWORDS, SearchResultSet, get_neighbors, search

logger = logging.getLogger(__name__)

DEFAULT_STUDY_TOP_K = 8
DEFAULT_STUDY_CONTEXT_MAX_TOKENS = 6000
DEFAULT_STUDY_MAX_OUTPUT_TOKENS = 1200
DEFAULT_STUDY_SUMMARY_MAX_CHUNKS = 40
DEFAULT_STUDY_GROUNDING_MIN_OVERLAP = 0.35
DEFAULT_STUDY_SOURCE_TYPES = ("arquivo", "pagina", "ementa")
TASK_SOURCE_KEYWORDS = (
    "tarefa",
    "trabalho",
    "atividade",
    "prazo",
    "enunciado",
    "rubrica",
    "aviso",
    "comunicado",
    "prova",
    "quiz",
)

_CITATION_RE = re.compile(r"\[F(\d+)\]")


def should_include_task_sources(query: str) -> bool:
    folded = unicodedata.normalize("NFKD", query or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    return any(keyword in folded for keyword in TASK_SOURCE_KEYWORDS)


def _task_has_body(text: str) -> bool:
    """Detecta chunks de tarefa que contêm apenas cabeçalho/metadados."""
    body_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.casefold()
        if stripped.startswith("#") or lowered.startswith(
            ("prazo:", "pontos:", "tipos de entrega:")
        ):
            continue
        body_lines.append(stripped)
    return bool(" ".join(body_lines).strip())


def extract_content_stems(text: str) -> set[str]:
    """Extrai radicais de palavras de conteúdo, ignorando stopwords e pontuação."""
    if not text:
        return set()
    normalized = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in normalized if not unicodedata.combining(c)).lower()
    cleaned = re.sub(r"[^\w\s]", " ", no_accents)
    stems: set[str] = set()
    for tok in cleaned.split():
        if len(tok) < 3 or tok in PORTUGUESE_STOPWORDS or tok.isdigit():
            continue
        if tok.endswith("oes") and len(tok) >= 6:
            base = tok[:-3]
        elif (
            tok.endswith("ao")
            and len(tok) >= 5
            or tok.endswith(("es", "as", "os", "is", "am", "em"))
            and len(tok) >= 5
        ):
            base = tok[:-2]
        elif tok.endswith(("s", "a", "o", "e")) and len(tok) >= 5:
            base = tok[:-1]
        else:
            base = tok
        stems.add(base if len(base) >= 3 else tok)
    return stems


def calculate_grounding_overlap(sentence: str, chunk_text: str) -> float:
    """Calcula a proporção de radicais de conteúdo da frase presentes no chunk."""
    sent_stems = extract_content_stems(sentence)
    if not sent_stems:
        return 1.0
    chunk_stems = extract_content_stems(chunk_text)
    overlap = len(sent_stems & chunk_stems)
    return overlap / len(sent_stems)


@dataclass(frozen=True, slots=True)
class ContextBlock:
    block_id: str  # ex.: "[F1]"
    chunk_id: int
    course_id: int
    course_name: str
    file_id: int
    filename: str
    location_label: str
    text: str
    score: float
    token_estimate: int
    source_type: str = "arquivo"
    due_at: str | None = None


@dataclass(frozen=True, slots=True)
class StudyContextPack:
    blocks: list[ContextBlock]
    context_text: str
    coverage: str  # "strong", "weak", "none"
    # "[F1]" -> (course_name, filename, location_label, text)
    sources_map: dict[str, tuple[str, ...]]
    total_tokens: int


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def build_context(
    repo: Repository,
    user_id: str,
    query: str,
    *,
    course_id: int | None = None,
    source_types: Sequence[str] | None = None,
    k: int = DEFAULT_STUDY_TOP_K,
    max_tokens: int = DEFAULT_STUDY_CONTEXT_MAX_TOKENS,
    expand_neighbors: bool = True,
) -> StudyContextPack:
    """Busca trechos relevantes e monta blocos numerados [F1], [F2]...

    Respeita o orçamento de tokens max_tokens e remove blocos redundantes
    (mesmo chunk_id ou mesmo arquivo/conteúdo duplicado).
    """
    if source_types is None:
        selected_types: Sequence[str] = DEFAULT_STUDY_SOURCE_TYPES
        if should_include_task_sources(query):
            selected_types = (*DEFAULT_STUDY_SOURCE_TYPES, "tarefa", "aviso")
    else:
        selected_types = source_types

    search_results: SearchResultSet = search(
        repo,
        user_id,
        query,
        course_id=course_id,
        source_types=selected_types,
        k=k,
    )

    if not search_results:
        return StudyContextPack(
            blocks=[],
            context_text="",
            coverage="none",
            sources_map={},
            total_tokens=0,
        )

    # Coleta chunks iniciais e opcionalmente expande vizinhos imediatos dos mais relevantes
    candidate_chunks: list[dict[str, Any]] = []
    seen_chunk_ids: set[int] = set()

    for item in search_results:
        if item.source_type == "tarefa" and not _task_has_body(item.text):
            continue
        if item.chunk_id not in seen_chunk_ids:
            seen_chunk_ids.add(item.chunk_id)
            candidate_chunks.append(
                {
                    "chunk_id": item.chunk_id,
                    "course_id": item.course_id,
                    "course_name": item.course_name,
                    "file_id": item.file_id,
                    "filename": item.filename,
                    "location_label": item.location_label,
                    "text": item.text,
                    "score": item.score,
                    "source_type": item.source_type,
                    "due_at": item.due_at,
                }
            )

        # Se habilitado, inclui até 1 vizinho adjacente para os 2 melhores resultados
        # (Apenas quando a cobertura não for fraca, para não puxar ruído de vizinhos irrelevantes)
        if expand_neighbors and search_results.coverage != "weak" and len(candidate_chunks) <= 2:
            try:
                neighbors = get_neighbors(repo, item.chunk_id, before=1, after=1)
                for n in neighbors:
                    n_id = int(n["id"])
                    if item.source_type == "tarefa" and not _task_has_body(n["text"]):
                        continue
                    if n_id not in seen_chunk_ids:
                        seen_chunk_ids.add(n_id)
                        candidate_chunks.append(
                            {
                                "chunk_id": n_id,
                                "course_id": item.course_id,
                                "course_name": item.course_name,
                                "file_id": item.file_id,
                                "filename": item.filename,
                                "location_label": n["location_label"],
                                "text": n["text"],
                                "score": item.score + 0.1,  # score levemente inferior ao do centro
                                "source_type": item.source_type,
                                "due_at": item.due_at,
                            }
                        )
            except Exception:
                logger.debug("Falha ao expandir vizinhos do chunk %s", item.chunk_id, exc_info=True)

    # Ordena por relevância (menor BM25 score primeiro)
    candidate_chunks.sort(key=lambda c: float(c.get("score", 999.0)))

    # Alocação com respeito ao orçamento de tokens
    allocated_blocks: list[ContextBlock] = []
    sources_map: dict[str, tuple[str, ...]] = {}
    accumulated_tokens = 0
    seen_text_hashes: set[str] = set()

    block_counter = 1
    for cand in candidate_chunks:
        text = cand["text"].strip()
        if cand.get("source_type") == "tarefa" and not _task_has_body(text):
            continue
        thash = text[:250]
        if thash in seen_text_hashes:
            continue
        seen_text_hashes.add(thash)

        tok_est = _estimate_tokens(text)
        if accumulated_tokens + tok_est > max_tokens and allocated_blocks:
            break

        block_id = f"[F{block_counter}]"
        c_name = short_course_name(cand["course_name"])
        f_name = cand["filename"]
        loc_lbl = cand["location_label"]

        allocated_blocks.append(
            ContextBlock(
                block_id=block_id,
                chunk_id=cand["chunk_id"],
                course_id=cand["course_id"],
                course_name=c_name,
                file_id=cand["file_id"],
                filename=f_name,
                location_label=loc_lbl,
                text=text,
                score=float(cand["score"]),
                token_estimate=tok_est,
                source_type=cand.get("source_type", "arquivo"),
                due_at=cand.get("due_at"),
            )
        )
        sources_map[block_id] = (c_name, f_name, loc_lbl, text)
        accumulated_tokens += tok_est
        block_counter += 1

    # Formata os blocos para o prompt
    type_tags = {
        "arquivo": "[Arquivo]",
        "pagina": "[Página]",
        "tarefa": "[Tarefa]",
        "aviso": "[Aviso]",
        "ementa": "[Ementa]",
    }
    formatted_parts: list[str] = []
    for blk in allocated_blocks:
        tag = type_tags.get(blk.source_type, "[Arquivo]")
        due_info = f" | Prazo: {blk.due_at}" if blk.due_at else ""
        header = (
            f"{blk.block_id} {tag} Matéria: {blk.course_name} | "
            f"Título: {blk.filename} | Local: {blk.location_label}{due_info}"
        )
        formatted_parts.append(f"{header}\n{blk.text}")

    context_str = "\n\n".join(formatted_parts)

    return StudyContextPack(
        blocks=allocated_blocks,
        context_text=context_str,
        coverage=search_results.coverage,
        sources_map=sources_map,
        total_tokens=accumulated_tokens,
    )


def build_study_system_prompt(coverage: str) -> str:
    """Retorna as instruções compartilhadas de fundamentação em português."""
    coverage_rule = ""
    if coverage in ("weak", "none"):
        coverage_rule = (
            "ATENÇÃO COBERTURA FRACA/NULA:\n"
            "O assunto da pergunta NÃO foi encontrado no material de aula.\n"
            "1. A PRIMEIRA LINHA da resposta DEVE ser estritamente:\n"
            "'Não encontrei esse assunto no material de aula.'\n"
            "2. NÃO descreva nem resuma trechos irrelevantes que apareceram por aproximação.\n"
            "3. NÃO cite fontes [Fn] para assuntos que não estão no material.\n"
            "4. Logo abaixo, forneça apenas uma explicação breve e direta de conhecimento geral "
            "com o cabeçalho 'Complemento (fora do material):' em no máximo 3 ou 4 linhas.\n\n"
        )

    return (
        "Você é um tutor acadêmico direto e objetivo no Telegram.\n"
        "Seu tom deve ser amigável, didático, focado e adequado para celular.\n\n"
        f"{coverage_rule}"
        "REGRAS INVIOLÁVEIS DE FUNDAMENTAÇÃO:\n"
        "1. Responda baseando-se estritamente nos blocos de contexto numerados [F1], [F2], etc.\n"
        "2. Sempre cite a fonte de cada afirmação usando o identificador exato [Fn] (ex.: [F1]).\n"
        "3. NUNCA invente fontes, autores, datas, páginas ou identificadores fora do contexto.\n"
        "4. NUNCA cite um [Fn] que não exista nos blocos fornecidos.\n"
        "5. Mantenha a explicação concisa (em torno de 10 a 12 linhas úteis, salvo resumo longo).\n"
    )


_MD_URL_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\[\]]+")


def sanitize_answer_urls(text: str, context_texts: Sequence[str]) -> str:
    """Remove links que não aparecem literalmente no contexto recuperado."""
    allowed = {normalize_url(url) for context in context_texts for url in extract_urls(context)}
    if not text:
        return text

    removed = 0

    def replace_markdown(match: re.Match[str]) -> str:
        nonlocal removed
        url = normalize_url(match.group(2))
        if url in allowed:
            return match.group(0)
        removed += 1
        return match.group(1)

    cleaned = _MD_URL_RE.sub(replace_markdown, text)

    def replace_bare(match: re.Match[str]) -> str:
        nonlocal removed
        url = normalize_url(match.group(0))
        if url in allowed:
            return match.group(0)
        removed += 1
        return ""

    cleaned = _BARE_URL_RE.sub(replace_bare, cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if removed:
        logger.warning("Removidos %s links sem suporte literal no contexto", removed)
    return cleaned


def post_process_study_answer(
    raw_answer: str,
    sources_map: dict[str, tuple[str, ...]],
    *,
    min_overlap: float = DEFAULT_STUDY_GROUNDING_MIN_OVERLAP,
    coverage: str = "strong",
    context_texts: Sequence[str] = (),
) -> str:
    """Pós-processa a resposta validando frase a frase a fundamentação nos chunks citados.

    - Para cada frase com [Fn]: calcula sobreposição de radicais de conteúdo com o chunk citado.
    - Se overlap < min_overlap: marca como '(fonte não confirmada)' em vez de sustentada.
    - Remove citações inválidas como [F99].
    - Se overlap >= min_overlap: substitui [Fn] por '(arquivo, Local)' legível.
    - A lista 'Fontes:' só inclui chunks que sustentaram ao menos uma frase confirmada.
    - Frases afirmativas sem citação são contadas; com cobertura strong, são marcadas quando
      não têm sobreposição mínima com nenhum bloco de contexto.
    """
    if not raw_answer:
        return ""

    sustained_keys: set[str] = set()
    total_evaluated = 0
    confirmed_count = 0
    unconfirmed_count = 0
    uncited_total = 0
    uncited_unconfirmed = 0

    def is_affirmative(sentence: str) -> bool:
        clean = sentence.strip()
        if not clean or clean.endswith(":"):
            return False
        if clean.startswith(("#", "•", "-", "*")):
            return False
        return len(extract_content_stems(clean)) >= 3

    def process_uncited(sentence: str) -> str:
        nonlocal uncited_total, uncited_unconfirmed
        if not is_affirmative(sentence):
            return sentence
        uncited_total += 1
        if coverage == "strong" and context_texts:
            max_overlap = max(
                (calculate_grounding_overlap(sentence, text) for text in context_texts),
                default=0.0,
            )
            if max_overlap < min_overlap:
                uncited_unconfirmed += 1
                return f"{sentence.rstrip()} (fonte não confirmada)"
        return sentence

    lines = raw_answer.split("\n")
    processed_lines: list[str] = []

    for line in lines:
        if not line.strip() or not _CITATION_RE.search(line):

            def _clean_fallback(m: re.Match[str]) -> str:
                k = f"[F{m.group(1)}]"
                return f"({sources_map[k][1]}, {sources_map[k][2]})" if k in sources_map else ""

            cleaned_line = _CITATION_RE.sub(_clean_fallback, line)
            if not line.strip():
                processed_lines.append(cleaned_line)
                continue
            sentences = re.split(r"(?<=[.!?])\s+", cleaned_line)
            processed_lines.append(" ".join(process_uncited(sent) for sent in sentences))
            continue

        raw_sentences = re.split(r"(?<=[.!?])\s+", line)
        processed_sentences: list[str] = []

        for sent in raw_sentences:
            citations = [int(m) for m in _CITATION_RE.findall(sent)]
            if not citations:
                processed_sentences.append(process_uncited(sent))
                continue

            valid_keys = [f"[F{c}]" for c in citations if f"[F{c}]" in sources_map]
            if not valid_keys:
                clean_sent = _CITATION_RE.sub("", sent).strip()
                processed_sentences.append(clean_sent)
                continue

            sent_clean = _CITATION_RE.sub("", sent).strip()
            chunks_text = " ".join(sources_map[k][3] for k in valid_keys if len(sources_map[k]) > 3)

            is_confirmed = True
            if chunks_text:
                overlap = calculate_grounding_overlap(sent_clean, chunks_text)
                is_confirmed = overlap >= min_overlap

            total_evaluated += 1
            if is_confirmed:
                confirmed_count += 1
                for k in valid_keys:
                    sustained_keys.add(k)

                def replace_valid(match: re.Match[str]) -> str:
                    k = f"[F{match.group(1)}]"
                    if k in sources_map:
                        _, f_name, loc = sources_map[k][:3]
                        return f"({f_name}, {loc})"
                    return ""

                p_sent = _CITATION_RE.sub(replace_valid, sent)
            else:
                unconfirmed_count += 1

                def replace_unconfirmed(match: re.Match[str]) -> str:
                    k = f"[F{match.group(1)}]"
                    if k in sources_map:
                        return "(fonte não confirmada)"
                    return ""

                p_sent = _CITATION_RE.sub(replace_unconfirmed, sent)

            processed_sentences.append(p_sent)

        processed_lines.append(" ".join(processed_sentences))

    processed = "\n".join(processed_lines)
    processed = re.sub(r"[ \t]+", " ", processed).strip()
    processed = sanitize_answer_urls(processed, context_texts)

    if total_evaluated > 0 or uncited_total > 0:
        logger.info(
            "Verificação de fundamentação: %s frases citadas avaliadas, %s confirmadas, "
            "%s não confirmadas; %s frases sem citação, %s marcadas (limiar %.2f, cobertura %s)",
            total_evaluated,
            confirmed_count,
            unconfirmed_count,
            uncited_total,
            uncited_unconfirmed,
            min_overlap,
            coverage,
        )

    # A lista 'Fontes:' só inclui chunks que sustentaram ao menos uma frase
    if sustained_keys:
        unique_sources: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()

        for k in sorted(sustained_keys, key=lambda x: int(x[2:-1])):
            if k in sources_map:
                c_name, f_name, loc = sources_map[k][:3]
                source_key = (f_name, loc)
                if source_key not in seen:
                    seen.add(source_key)
                    unique_sources.append((c_name, f_name, loc))

        if unique_sources:
            lines_out = ["\n\n<b>Fontes:</b>"]
            for _c_name, f_name, loc in unique_sources:
                lines_out.append(f"- {f_name} ({loc})")
            processed = processed + "\n".join(lines_out)

    return processed
