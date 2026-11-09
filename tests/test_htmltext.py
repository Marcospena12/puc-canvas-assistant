from __future__ import annotations

from canvas_assistant.htmltext import html_to_text, truncate


def test_html_to_text_removes_tags_and_scripts() -> None:
    html = "<p>Olá <b>mundo</b></p><script>alert('x')</script><ul><li>um</li><li>dois</li></ul>"

    text = html_to_text(html)

    assert "Olá mundo" in text
    assert "alert" not in text
    assert "um" in text
    assert "dois" in text


def test_html_to_text_decodes_entities_and_limits() -> None:
    text = html_to_text("<p>Tom &amp; Jerry</p>", limit=8)

    assert text.endswith("…")
    assert len(text) == 8


def test_html_to_text_none() -> None:
    assert html_to_text(None) == ""


def test_truncate() -> None:
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdef", 5) == "abcd…"
