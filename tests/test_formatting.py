from __future__ import annotations

from canvas_assistant.channels.formatting import format_llm_for_telegram, to_telegram_html


def test_formats_bold_italic_code_and_links() -> None:
    text = "**Atenção** para *hoje*: `1404750` em [abrir](https://canvas.test/a?b=1&c=2)."

    html = to_telegram_html(text)

    assert html == (
        "<b>Atenção</b> para <i>hoje</i>: <code>1404750</code> em "
        '<a href="https://canvas.test/a?b=1&amp;c=2">abrir</a>.'
    )


def test_link_is_removed_when_not_in_allowed_urls() -> None:
    html = to_telegram_html(
        "Veja [aqui](https://canvas.test/assignments/999).",
        allowed_urls={"https://canvas.test/assignments/100"},
    )

    assert "canvas.test" not in html
    assert "aqui" in html


def test_link_allows_equivalent_trailing_slash() -> None:
    html = to_telegram_html(
        "[aqui](https://canvas.test/a/)",
        allowed_urls={"https://canvas.test/a"},
    )

    assert '<a href="https://canvas.test/a/">aqui</a>' in html


def test_escapes_html_and_does_not_format_inside_code() -> None:
    html = to_telegram_html("<script>alert(1)</script> e `**não**`")

    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "<code>**não**</code>" in html
    assert "<b>não</b>" not in html


def test_removes_markdown_headings() -> None:
    assert to_telegram_html("### Título\n- item") == "Título\n- item"


def test_format_llm_for_telegram_converts_headers_and_bullets() -> None:
    raw = "## Seção Importante\n- item 1\n* item 2\n---\n"
    formatted = format_llm_for_telegram(raw)
    assert "**Seção Importante**" in formatted
    assert "• item 1" in formatted
    assert "• item 2" in formatted
    assert "---" not in formatted


def test_to_telegram_html_escapes_unsafe_brackets_and_ampersand() -> None:
    raw = "Exemplo: 5 < 10 & 20 > 15 com <b>negrito</b> e <malicioso>tag</malicioso>"
    html = to_telegram_html(raw)
    assert "5 &lt; 10 &amp; 20 &gt; 15" in html
    assert "<b>negrito</b>" in html
    assert "&lt;malicioso&gt;tag&lt;/malicioso&gt;" in html
