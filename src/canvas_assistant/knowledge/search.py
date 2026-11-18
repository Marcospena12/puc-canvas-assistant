from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from ..repository import Repository

PORTUGUESE_STOPWORDS = frozenset(
    {
        "a",
        "ao",
        "aos",
        "aquela",
        "aquelas",
        "aquele",
        "aqueles",
        "aquilo",
        "as",
        "ate",
        "com",
        "como",
        "da",
        "das",
        "de",
        "dela",
        "delas",
        "dele",
        "deles",
        "depois",
        "do",
        "dos",
        "e",
        "ela",
        "elas",
        "ele",
        "eles",
        "em",
        "entre",
        "era",
        "eram",
        "essa",
        "essas",
        "esse",
        "esses",
        "esta",
        "estas",
        "este",
        "estes",
        "eu",
        "foi",
        "fomos",
        "foram",
        "isso",
        "isto",
        "ja",
        "lhe",
        "lhes",
        "mais",
        "mas",
        "me",
        "mesmo",
        "meu",
        "meus",
        "minha",
        "minhas",
        "muito",
        "na",
        "nas",
        "nao",
        "no",
        "nos",
        "nossa",
        "nossas",
        "nosso",
        "nossos",
        "num",
        "numa",
        "o",
        "os",
        "ou",
        "para",
        "pela",
        "pelas",
        "pelo",
        "pelos",
        "por",
        "qual",
        "quando",
        "que",
        "quem",
        "se",
        "sem",
        "seu",
        "seus",
        "so",
        "sua",
        "suas",
        "tambem",
        "te",
        "tem",
        "ter",
        "teu",
        "teus",
        "tu",
        "tua",
        "tuas",
        "um",
        "uma",
        "umas",
        "uns",
        "voce",
        "voces",
    }
)

_SANITIZE_RE = re.compile(r"""[^\w\s]""", re.UNICODE)


def _fold(term: str) -> str:
    normalized = unicodedata.normalize("NFKD", term or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower()


def canonical_term(term: str) -> str:
    """Canonicaliza flexões comuns sem perder a distinção entre classes.

    O FTS5 continua usando prefixos para recall. Esta função é aplicada
    depois, no Python, para confirmar que o documento contém o termo e não
    apenas um prefixo parecido. Exemplos:
      - dimensões/dimensão -> dimensao
      - tabelas/tabela -> tabela
      - avaliação/avaliações -> avalia
      - funcionais/funcional -> funcional
      - funcionando -> funcion (não casa com funcional)
    """
    clean = _fold(term)
    clean = re.sub(r"[^\w]", "", clean)
    if not clean:
        return ""

    if clean.endswith(("ando", "endo", "indo")) and len(clean) >= 7:
        clean = clean[:-4]
    elif clean.endswith("coes") and len(clean) >= 6:
        clean = clean[:-4] + "cao"

    if clean.endswith("cao") and len(clean) >= 5:
        clean = clean[:-3] + "ca"
    elif (clean.endswith("oes") and len(clean) >= 6) or (clean.endswith("aes") and len(clean) >= 5):
        clean = clean[:-3] + "ao"
    elif clean.endswith("ais") and len(clean) >= 5:
        clean = clean[:-3] + "al"
    elif clean.endswith("eis") and len(clean) >= 5:
        clean = clean[:-3] + "el"
    elif clean.endswith("ois") and len(clean) >= 5:
        clean = clean[:-3] + "ol"
    elif clean.endswith("ares") and len(clean) >= 6:
        clean = clean[:-4] + "ar"
    elif clean.endswith("ores") and len(clean) >= 6:
        clean = clean[:-4] + "or"
    elif clean.endswith("ns") and len(clean) >= 5:
        clean = clean[:-2] + "m"
    elif clean.endswith("s") and len(clean) >= 5:
        clean = clean[:-1]

    if clean.endswith(("ea", "eo")) and len(clean) >= 6:
        clean = clean[:-1]
    return clean


def stem_prefix_pt(term: str) -> str:
    """Heurística leve de lematização por prefixo para o português.

    Para termos com 4 ou mais letras, remove sufixos flexionados comuns
    (plurais, desinências nominais e verbais como 'oes', 'es', 's', 'am', 'em', 'as', 'os')
    e adiciona asterisco para busca por prefixo no FTS5. Termos muito curtos (< 4 letras)
    são mantidos na íntegra para evitar explosão de falsos positivos.

    Exemplos:
      - 'dimensoes' -> 'dimens*'
      - 'dimensao'  -> 'dimens*'
      - 'tabelas'   -> 'tabel*'
      - 'modelos'   -> 'model*'
      - 'redes'     -> 'red*'
      - 'dado'      -> 'dado*'
      - 'banco'     -> 'banc*'
      - 'sql'       -> 'sql'
    """
    clean = _fold(term)

    if len(clean) < 4:
        return f'"{clean}"'

    # Remoção de terminações plurais e verbais comuns
    if clean.endswith("oes") and len(clean) >= 6:
        base = clean[:-3]
    elif (
        clean.endswith("ao")
        and len(clean) >= 5
        or clean.endswith(("es", "as", "os", "is", "am", "em"))
        and len(clean) >= 5
    ):
        base = clean[:-2]
    elif clean.endswith(("s", "a", "o", "e")) and len(clean) >= 5:
        base = clean[:-1]
    else:
        base = clean

    # Garante um radical mínimo de 3 letras para prefixar com segurança
    if len(base) >= 3:
        return f'"{base}"*'
    return f'"{clean}"'


def extract_query_terms(raw_query: str) -> list[str]:
    """Extrai os termos úteis da query descartando stopwords e termos reservados."""
    if not raw_query or not raw_query.strip():
        return []

    normalized = unicodedata.normalize("NFKD", raw_query)
    no_accents = "".join(c for c in normalized if not unicodedata.combining(c)).lower()
    cleaned = _SANITIZE_RE.sub(" ", no_accents)

    reserved = {"and", "or", "not", "near"}
    tokens: list[str] = []
    for token in cleaned.split():
        if token in reserved or token in PORTUGUESE_STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def prepare_fts_query(raw_query: str) -> tuple[str, str]:
    """Limpa e formata a query do usuário para consumo seguro no SQLite FTS5.

    Remove pontuação e caracteres de sintaxe FTS (", ', (, ), *, -, :, etc.),
    descarta stopwords em português e produz duas variantes da query:
    1. AND_QUERY: todos os termos combinados com AND (maior precisão).
    2. OR_QUERY: todos os termos combinados com OR (maior abrangência).
    """
    tokens = extract_query_terms(raw_query)
    if not tokens:
        return "", ""

    term_clauses = [stem_prefix_pt(tok) for tok in tokens]
    and_query = " AND ".join(term_clauses)
    or_query = " OR ".join(term_clauses)
    return and_query, or_query


@dataclass(frozen=True, slots=True)
class SearchResultItem:
    chunk_id: int
    course_id: int
    course_name: str
    file_id: int
    filename: str
    location_label: str
    score: float
    snippet: str
    sha256: str
    text: str
    associated_courses: tuple[str, ...] = ()
    match_mode: str = "all_terms"  # "all_terms" ou "partial"
    matched_terms: int = 1
    total_terms: int = 1
    source_type: str = "arquivo"
    source_ref: str = ""
    due_at: str | None = None


@dataclass(frozen=True, slots=True)
class SearchResultSet:
    items: list[SearchResultItem]
    coverage: str  # "strong", "weak" ou "none"
    total_terms: int = 0

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]

    def __bool__(self) -> bool:
        return bool(self.items)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return self.items == other
        if isinstance(other, SearchResultSet):
            return (
                self.items == other.items
                and self.coverage == other.coverage
                and self.total_terms == other.total_terms
            )
        return False


def _format_snippet(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) > 280:
        return cleaned[:277].rstrip() + "…"
    return cleaned


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    folded: list[str] = []
    positions: list[int] = []
    for idx, char in enumerate(text):
        decomposed = unicodedata.normalize("NFKD", char)
        base = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
        for item in base:
            folded.append(item)
            positions.append(idx)
    return "".join(folded), positions


def _canonical_spans(text: str, canonical_terms: list[str]) -> list[tuple[int, int, int]]:
    folded, positions = _fold_with_map(text)
    wanted: dict[str, int] = {}
    for idx, term in enumerate(canonical_terms):
        if term:
            wanted.setdefault(term, idx)
    spans: list[tuple[int, int, int]] = []
    for match in re.finditer(r"[^\W_]+", folded):
        canonical = canonical_term(match.group(0))
        token_idx = wanted.get(canonical)
        if token_idx is None:
            continue
        start = positions[match.start()]
        end = positions[match.end() - 1] + 1
        spans.append((start, end, token_idx))
    return sorted(spans)


def _token_occurrences(text: str, tokens: list[str]) -> list[tuple[int, int, int]]:
    return _canonical_spans(text, [canonical_term(tok) for tok in tokens])


def _highlight_window(window: str, tokens: list[str]) -> str:
    spans = [
        (start, end)
        for start, end, _idx in _canonical_spans(window, [canonical_term(tok) for tok in tokens])
    ]
    if not spans:
        return window
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        parts.append(window[cursor:start])
        parts.append(f"<b>{window[start:end]}</b>")
        cursor = end
    parts.append(window[cursor:])
    return "".join(parts)


def _extract_authentic_snippet(text: str, fts_snippet: str | None, tokens: list[str]) -> str:
    """Escolhe a janela literal do chunk que contém o maior número de termos casados."""
    occurrences = _token_occurrences(text, tokens)
    if not occurrences:
        if fts_snippet:
            raw_core = re.sub(r"</?b>", "", fts_snippet).replace("...", "").strip()
            first_line = raw_core.splitlines()[0].strip() if raw_core else ""
            if first_line and first_line in text:
                return fts_snippet
        return _format_snippet(text)

    window_size = 260
    best: tuple[int, int, int] = (-1, 0, 0)
    for anchor, _end, _tok in occurrences:
        start = max(0, anchor - 70)
        end = min(len(text), start + window_size)
        distinct = len({tok for s, _e, tok in occurrences if start <= s < end})
        if distinct > best[0]:
            best = (distinct, start, end)

    if len(occurrences) == 1:
        start, end = best[1], best[2]
    else:
        first = occurrences[0][0]
        last = max(e for _s, e, _t in occurrences)
        if last - first <= window_size:
            start = max(0, first - 40)
            end = min(len(text), max(last + 40, start + window_size))
        else:
            start, end = best[1], best[2]

    while start > 0 and text[start - 1].isalnum():
        start -= 1
    while end < len(text) and text[end].isalnum():
        end += 1

    window = text[start:end]
    highlighted = _highlight_window(window, tokens)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{highlighted}{suffix}"


def search(
    repo: Repository,
    user_id: str,
    query: str,
    *,
    course_id: int | None = None,
    file_types: Sequence[str] | None = None,
    source_types: Sequence[str] | None = None,
    k: int = 8,
) -> SearchResultSet:
    """Interface de busca semântica e por relevância usando SQLite FTS5.

    Aplica BM25 com peso maior no título (5.0) do que no texto (1.0).
    Estratégia: tenta casamento estrito (AND); se insuficiente (< k), completa
    com os melhores do casamento brando (OR). Colapsa itens duplicados
    por sha256 indicando todas as matérias associadas.
    """
    useful_tokens = extract_query_terms(query)
    total_terms = len(useful_tokens)
    if total_terms == 0:
        return SearchResultSet(items=[], coverage="none", total_terms=0)
    canonical_terms = [canonical_term(tok) for tok in useful_tokens]

    and_query, or_query = prepare_fts_query(query)
    if not and_query:
        return SearchResultSet(items=[], coverage="none", total_terms=0)

    def execute_fts(fts_match: str, limit_count: int) -> list[dict[str, Any]]:
        clauses = [
            "fc.user_id = ?",
            "c.active = 1",
            (
                "(fc.source_type != 'arquivo' OR EXISTS ("
                "SELECT 1 FROM file_sync_state fs "
                "WHERE fs.user_id = fc.user_id AND fs.file_id = fc.file_id AND fs.active = 1))"
            ),
            (
                "(fc.source_type = 'arquivo' OR EXISTS ("
                "SELECT 1 FROM canvas_content_state cs "
                "WHERE cs.user_id = fc.user_id AND cs.course_id = fc.course_id "
                "AND cs.source_type = fc.source_type AND cs.source_ref = fc.source_ref "
                "AND cs.active = 1))"
            ),
        ]
        params: list[Any] = [fts_match, user_id]

        if course_id is not None:
            clauses.append("fc.course_id = ?")
            params.append(course_id)

        if source_types:
            ph = ",".join("?" for _ in source_types)
            clauses.append(f"fc.source_type IN ({ph})")
            params.extend(source_types)

        if file_types:
            ext_clauses = []
            for ext in file_types:
                ext_clauses.append("fc.title LIKE ?")
                params.append(f"%.{ext.lstrip('.')}")
            clauses.append(f"({' OR '.join(ext_clauses)})")

        where_str = " AND ".join(clauses)

        sql = f"""
            SELECT
                fc.id AS chunk_id,
                fc.course_id,
                c.name AS course_name,
                fc.file_id,
                COALESCE(fc.title, '') AS filename,
                fc.source_type,
                fc.source_ref,
                fc.due_at,
                fc.location_label,
                bm25(chunks_fts, 5.0, 1.0, 1.0) AS rank_score,
                snippet(chunks_fts, 2, '<b>', '</b>', '...', 22) AS fts_snippet,
                fc.sha256,
                fc.text
            FROM chunks_fts
            JOIN file_chunks fc ON fc.id = chunks_fts.rowid
            JOIN courses c ON c.user_id = fc.user_id AND c.id = fc.course_id
            WHERE chunks_fts MATCH ? AND {where_str}
            ORDER BY rank_score ASC
            LIMIT ?
        """
        params.append(limit_count)

        with closing(repo.db.connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    raw_and_items = execute_fts(and_query, k * 2)
    raw_items = list(raw_and_items)

    if len(raw_items) < k and or_query != and_query:
        existing_ids = {r["chunk_id"] for r in raw_items}
        or_items = execute_fts(or_query, k * 2)
        for item in or_items:
            if item["chunk_id"] not in existing_ids:
                raw_items.append(item)
                existing_ids.add(item["chunk_id"])

    if not raw_items:
        return SearchResultSet(items=[], coverage="none", total_terms=total_terms)

    # Mapa de matérias por sha256 para desambiguação e deduplicação
    all_sha = list({r["sha256"] for r in raw_items if r.get("sha256")})
    sha_courses_map: dict[str, list[str]] = {}
    if all_sha:
        ph = ",".join("?" for _ in all_sha)
        with closing(repo.db.connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT fs.sha256, c.name AS course_name
                FROM file_sync_state fs
                JOIN courses c ON c.user_id = fs.user_id AND c.id = fs.course_id
                WHERE fs.user_id = ? AND fs.active = 1 AND c.active = 1
                  AND fs.sha256 IN ({ph})
                ORDER BY c.name COLLATE NOCASE
                """,
                [user_id, *all_sha],
            ).fetchall()
            for r in rows:
                sha_courses_map.setdefault(str(r["sha256"]), []).append(str(r["course_name"]))

    # Deduplicação de chunks idênticos em arquivos iguais entre matérias
    deduped_results: list[SearchResultItem] = []
    seen_text_hashes: set[str] = set()

    for r in raw_items:
        thash = r.get("text") or ""
        sha = r.get("sha256") or ""
        dedupe_key = f"{sha}:{thash[:200]}"
        if dedupe_key in seen_text_hashes:
            continue
        seen_text_hashes.add(dedupe_key)

        courses_for_sha = tuple(sha_courses_map.get(sha, [r["course_name"]]))

        doc_norm = _fold(f"{r['filename']} {r['text']}")
        doc_canonical = {canonical_term(tok) for tok in re.findall(r"[^\W_]+", doc_norm)}
        matched_terms = sum(1 for term in canonical_terms if term in doc_canonical)
        if matched_terms == 0:
            continue
        match_mode = "all_terms" if matched_terms == total_terms else "partial"
        snippet = _extract_authentic_snippet(r["text"], r.get("fts_snippet"), useful_tokens)

        deduped_results.append(
            SearchResultItem(
                chunk_id=r["chunk_id"],
                course_id=r["course_id"],
                course_name=r["course_name"],
                file_id=r["file_id"],
                filename=r["filename"],
                location_label=r["location_label"],
                score=round(float(r["rank_score"]), 4),
                snippet=snippet,
                sha256=sha,
                text=r["text"],
                associated_courses=courses_for_sha,
                match_mode=match_mode,
                matched_terms=matched_terms,
                total_terms=total_terms,
                source_type=r.get("source_type") or "arquivo",
                source_ref=str(r.get("source_ref") or ""),
                due_at=r.get("due_at"),
            )
        )

        if len(deduped_results) >= k:
            break

    coverage = "none"
    if deduped_results:
        has_all = any(it.match_mode == "all_terms" for it in deduped_results)
        coverage = "strong" if has_all else "weak"

    return SearchResultSet(items=deduped_results, coverage=coverage, total_terms=total_terms)


def get_neighbors(
    repo: Repository,
    chunk_id: int,
    *,
    before: int = 1,
    after: int = 1,
) -> list[dict[str, Any]]:
    """Devolve os chunks adjacentes do mesmo arquivo (usado na 3.4 para contexto expandido)."""
    return repo.get_neighbor_chunks(chunk_id, before=before, after=after)
