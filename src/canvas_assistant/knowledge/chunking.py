from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .extractors import DocumentUnit

TARGET_CHUNK_CHARS = 2400  # ~600 tokens
MAX_CHUNK_CHARS = 3600  # ~900 tokens
OVERLAP_CHARS = 300  # ~75 tokens
MAX_SLIDES_PER_CHUNK = 6  # Limite máximo de slides agrupados em um mesmo chunk
MIN_CHUNK_CHARS = 200  # Chunks menores que isso são mesclados com o vizinho
NOISE_CHUNK_CHARS = 40  # Chunks menores que 40 chars isolados são descartados como ruído


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_index: int
    location_type: str
    location_label: str
    location_start: int
    location_end: int
    text: str
    text_hash: str
    char_count: int
    token_estimate: int


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _split_long_text(
    text: str, max_chars: int = MAX_CHUNK_CHARS, overlap: int = OVERLAP_CHARS
) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    total = len(text)

    while start < total:
        end = min(start + max_chars, total)
        if end < total:
            split_point = text.rfind("\n\n", start, end)
            if split_point == -1 or split_point < start + (max_chars // 2):
                split_point = text.rfind("\n", start, end)
            if split_point == -1 or split_point < start + (max_chars // 2):
                split_point = text.rfind(". ", start, end)
                if split_point != -1:
                    split_point += 1
            if split_point == -1 or split_point < start + (max_chars // 2):
                split_point = text.rfind(" ", start, end)
            if split_point != -1 and split_point >= start + (max_chars // 2):
                end = split_point

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)

        if end >= total:
            break
        next_start = max(0, end - overlap)
        space_idx = text.find(" ", next_start, end)
        start = space_idx + 1 if space_idx != -1 and space_idx > start else end

    return chunks


def _format_slide_label(included_slide_nums: list[int]) -> str:
    if not included_slide_nums:
        return "Slide"
    if len(included_slide_nums) == 1:
        return f"Slide {included_slide_nums[0]}"
    # Se os slides forem estritamente consecutivos:
    if included_slide_nums[-1] - included_slide_nums[0] + 1 == len(included_slide_nums):
        return f"Slides {included_slide_nums[0]}-{included_slide_nums[-1]}"
    return f"Slides {', '.join(str(n) for n in included_slide_nums)}"


def build_chunks(
    units: list[DocumentUnit], *, max_slides: int = MAX_SLIDES_PER_CHUNK
) -> list[Chunk]:
    if not units:
        return []

    raw_chunks: list[tuple[str, str, int, int, str]] = []
    is_slide_doc = all(u.location_type == "slide" for u in units)

    if is_slide_doc:
        current_texts: list[str] = []
        current_slide_nums: list[int] = []
        current_len = 0

        for unit in units:
            unit_len = len(unit.text)
            exceeds_len = current_len > 0 and (current_len + unit_len > TARGET_CHUNK_CHARS)
            exceeds_count = len(current_slide_nums) >= max_slides

            if exceeds_len or exceeds_count:
                combined = "\n\n".join(current_texts)
                label = _format_slide_label(current_slide_nums)
                pieces = _split_long_text(combined)
                total_pieces = len(pieces)
                for p_idx, piece in enumerate(pieces, start=1):
                    p_label = (
                        label if total_pieces == 1 else f"{label} (parte {p_idx}/{total_pieces})"
                    )
                    raw_chunks.append(
                        ("slide", p_label, current_slide_nums[0], current_slide_nums[-1], piece)
                    )
                current_texts = [unit.text]
                current_slide_nums = [unit.index_start]
                current_len = unit_len
            else:
                current_texts.append(unit.text)
                current_slide_nums.append(unit.index_start)
                current_len += unit_len + 2

        if current_texts:
            combined = "\n\n".join(current_texts)
            label = _format_slide_label(current_slide_nums)
            pieces = _split_long_text(combined)
            total_pieces = len(pieces)
            for p_idx, piece in enumerate(pieces, start=1):
                p_label = label if total_pieces == 1 else f"{label} (parte {p_idx}/{total_pieces})"
                raw_chunks.append(
                    ("slide", p_label, current_slide_nums[0], current_slide_nums[-1], piece)
                )

    else:
        for unit in units:
            pieces = _split_long_text(unit.text)
            total_pieces = len(pieces)
            for part_idx, piece in enumerate(pieces, start=1):
                label = unit.label
                if total_pieces > 1:
                    label = f"{unit.label} (parte {part_idx}/{total_pieces})"
                raw_chunks.append(
                    (unit.location_type, label, unit.index_start, unit.index_end, piece)
                )

    # Mesclagem de chunks curtos (< MIN_CHUNK_CHARS) e descarte de ruído (< NOISE_CHUNK_CHARS)
    merged_chunks: list[tuple[str, str, int, int, str]] = []
    for loc_type, loc_label, start_idx, end_idx, text in raw_chunks:
        # Se for minúsculo e primeiro elemento ou isolado, descartar ruído óbvio
        if len(text) < NOISE_CHUNK_CHARS and not merged_chunks:
            continue

        if merged_chunks:
            prev_type, prev_label, prev_start, prev_end, prev_text = merged_chunks[-1]
            # Se o anterior for muito curto, mescla com o atual se forem do mesmo tipo
            if len(prev_text) < MIN_CHUNK_CHARS and prev_type == loc_type:
                new_start = min(prev_start, start_idx)
                new_end = max(prev_end, end_idx)
                new_label = prev_label if prev_label == loc_label else f"{prev_label}; {loc_label}"
                combined = f"{prev_text}\n\n{text}"
                merged_chunks[-1] = (prev_type, new_label, new_start, new_end, combined)
                continue
            # Se o atual for muito curto, mescla para dentro do anterior se couber
            if (
                len(text) < MIN_CHUNK_CHARS
                and prev_type == loc_type
                and len(prev_text) + len(text) <= MAX_CHUNK_CHARS
            ):
                new_start = min(prev_start, start_idx)
                new_end = max(prev_end, end_idx)
                new_label = prev_label if prev_label == loc_label else f"{prev_label}; {loc_label}"
                combined = f"{prev_text}\n\n{text}"
                merged_chunks[-1] = (prev_type, new_label, new_start, new_end, combined)
                continue

        merged_chunks.append((loc_type, loc_label, start_idx, end_idx, text))

    # Filtro final de ruído residual que não pôde ser mesclado
    final_items = [
        item
        for item in merged_chunks
        if len(item[4]) >= NOISE_CHUNK_CHARS or len(merged_chunks) == 1
    ]

    result: list[Chunk] = []
    for idx, (loc_type, loc_label, start_idx, end_idx, text) in enumerate(final_items, start=1):
        thash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        c_len = len(text)
        result.append(
            Chunk(
                chunk_index=idx,
                location_type=loc_type,
                location_label=loc_label,
                location_start=start_idx,
                location_end=end_idx,
                text=text,
                text_hash=thash,
                char_count=c_len,
                token_estimate=_estimate_tokens(text),
            )
        )

    return result
