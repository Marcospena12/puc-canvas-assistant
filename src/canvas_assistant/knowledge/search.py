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


def stem_prefix_pt(term: str) -> str:
    """Heurística leve de lematização por prefixo para o português.

    Para termos com 5 ou mais letras, remove sufixos flexionados comuns
    (plurais, desinências nominais e verbais como 'oes', 'es', 's', 'am', 'em', 'as', 'os')
    e adiciona asterisco para busca por prefixo no FTS5. Termos curtos (< 5 letras)
    são mantidos na íntegra para evitar explosão de falsos positivos.

    Exemplos:
      - 'dimensoes' -> 'dimens*'
      - 'dimensao'  -> 'dimens*'
      - 'tabelas'   -> 'tabel*'
      - 'modelos'   -> 'model*'
      - 'redes'     -> 'red*'
      - 'banco'     -> 'banc*'
      - 'sql'       -> 'sql'
    """
    clean = term.lower()
    # Remove acentos antes de avaliar sufixos
    nfkd = unicodedata.normalize("NFKD", clean)
    clean = "".join(c for c in nfkd if not unicodedata.combining(c))

    if len(clean) < 5:
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


def prepare_fts_query(raw_query: str) -> tuple[str, str]:
    """Limpa e formata a query do usuário para consumo seguro no SQLite FTS5.

    Remove pontuação e caracteres de sintaxe FTS (", ', (, ), *, -, :, etc.),
    descarta stopwords em português e produz duas variantes da query:
    1. AND_QUERY: todos os termos combinados com AND (maior precisão).
    2. OR_QUERY: todos os termos combinados com OR (maior abrangência).
    """
    if not raw_query or not raw_query.strip():
        return "", ""

    # Remove acentos e pontuação hostil
    normalized = unicodedata.normalize("NFKD", raw_query)
    no_accents = "".join(c for c in normalized if not unicodedata.combining(c)).lower()
    cleaned = _SANITIZE_RE.sub(" ", no_accents)

    # Ignora palavras reservadas do FTS5
    reserved = {"and", "or", "not", "near"}

    tokens: list[str] = []
    for token in cleaned.split():
        if token in reserved:
            continue
        if token in PORTUGUESE_STOPWORDS:
            continue
        tokens.append(token)

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


def _format_snippet(text: str) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) > 280:
        return cleaned[:277].rstrip() + "…"
    return cleaned


def search(
    repo: Repository,
    user_id: str,
    query: str,
    *,
    course_id: int | None = None,
    file_types: Sequence[str] | None = None,
    k: int = 8,
) -> list[SearchResultItem]:
    """Interface de busca semântica e por relevância usando SQLite FTS5.

    Aplica BM25 com peso maior no nome do arquivo (5.0) do que no texto (1.0).
    Estratégia: tenta casamento estrito (AND); se insuficiente (< k), completa
    com os melhores do casamento brando (OR). Colapsa arquivos duplicados
    por sha256 indicando todas as matérias associadas.
    """
    and_query, or_query = prepare_fts_query(query)
    if not and_query:
        return []

    def execute_fts(fts_match: str, limit_count: int) -> list[dict[str, Any]]:
        clauses = ["fc.user_id = ?", "c.active = 1", "fs.active = 1"]
        params: list[Any] = [fts_match, user_id]

        if course_id is not None:
            clauses.append("fc.course_id = ?")
            params.append(course_id)

        if file_types:
            ext_clauses = []
            for ext in file_types:
                ext_clauses.append("fs.display_name LIKE ?")
                params.append(f"%.{ext.lstrip('.')}")
            clauses.append(f"({' OR '.join(ext_clauses)})")

        where_str = " AND ".join(clauses)

        sql = f"""
            SELECT
                fc.id AS chunk_id,
                fc.course_id,
                c.name AS course_name,
                fc.file_id,
                COALESCE(fs.display_name, '') AS filename,
                fc.location_label,
                bm25(chunks_fts, 5.0, 1.0, 1.0) AS rank_score,
                snippet(chunks_fts, 2, '<b>', '</b>', '...', 22) AS fts_snippet,
                fc.sha256,
                fc.text
            FROM chunks_fts
            JOIN file_chunks fc ON fc.id = chunks_fts.rowid
            JOIN courses c ON c.user_id = fc.user_id AND c.id = fc.course_id
            JOIN file_sync_state fs ON fs.user_id = fc.user_id AND fs.file_id = fc.file_id
            WHERE chunks_fts MATCH ? AND {where_str}
            ORDER BY rank_score ASC
            LIMIT ?
        """
        params.append(limit_count)

        with closing(repo.db.connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    raw_items = execute_fts(and_query, k * 2)

    if len(raw_items) < k and or_query != and_query:
        existing_ids = {r["chunk_id"] for r in raw_items}
        or_items = execute_fts(or_query, k * 2)
        for item in or_items:
            if item["chunk_id"] not in existing_ids:
                raw_items.append(item)
                existing_ids.add(item["chunk_id"])

    if not raw_items:
        return []

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
        snippet = r.get("fts_snippet") or _format_snippet(r["text"])

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
            )
        )

        if len(deduped_results) >= k:
            break

    return deduped_results


def get_neighbors(
    repo: Repository,
    chunk_id: int,
    *,
    before: int = 1,
    after: int = 1,
) -> list[dict[str, Any]]:
    """Devolve os chunks adjacentes do mesmo arquivo (usado na 3.4 para contexto expandido)."""
    return repo.get_neighbor_chunks(chunk_id, before=before, after=after)
