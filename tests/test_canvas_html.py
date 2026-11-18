from __future__ import annotations

from canvas_assistant.knowledge.canvas_html import clean_canvas_html


def test_clean_canvas_html_extracts_media() -> None:
    html = (
        "<h3>Vídeo da Aula</h3>"
        '<iframe src="https://pucminas.instructuremedia.com/lti/launch?custom_arc_media_id=123" '
        'title="Apresentação da Disciplina"></iframe>'
    )
    res = clean_canvas_html(html)

    assert len(res.media) == 1
    assert res.media[0][0] == "iframe"
    assert "pucminas.instructuremedia.com" in res.media[0][1]
    assert res.media[0][2] == "Apresentação da Disciplina"
    # iframe LTI de videoaula com title gera marcador [Vídeo: <título>]
    assert "[Vídeo: Apresentação da Disciplina]" in res.text


def test_clean_canvas_html_extracts_lti_title_from_url() -> None:
    html = (
        '<iframe src="https://pucminas.instructuremedia.com/lti/launch?custom_arc_media_id=456'
        '&title=Aula%20sobre%20React%20Native"></iframe>'
    )
    res = clean_canvas_html(html)
    assert len(res.media) == 1
    assert res.media[0][2] == "Aula sobre React Native"
    assert "[Vídeo: Aula sobre React Native]" in res.text


def test_clean_canvas_html_decomposes_scripts_and_styles() -> None:
    html = (
        "<p>Conteúdo real.</p>"
        "<script>console.log('malicious');</script>"
        "<style>.hide { display: none; }</style>"
    )
    res = clean_canvas_html(html)

    assert "Conteúdo real." in res.text
    assert "malicious" not in res.text
    assert "display: none" not in res.text


def test_clean_canvas_html_formats_tables_and_lists() -> None:
    html = (
        "<table>"
        "<tr><th>Cabeçalho 1</th><th>Cabeçalho 2</th></tr>"
        "<tr><td>Dado 1</td><td>Dado 2</td></tr>"
        "</table>"
        "<ul>"
        "<li>Item 1</li>"
        "<li>Item 2</li>"
        "</ul>"
    )
    res = clean_canvas_html(html)

    assert "Cabeçalho 1 | Cabeçalho 2" in res.text
    assert "Dado 1 | Dado 2" in res.text
    assert "• Item 1" in res.text
    assert "• Item 2" in res.text


def test_clean_canvas_html_handles_images_and_alt() -> None:
    html = '<img src="diagrama.png" alt="Diagrama de Blocos"/><img src="decorativo.png"/>'
    res = clean_canvas_html(html)

    assert "[Imagem: Diagrama de Blocos]" in res.text
    assert res.images_without_alt == 1


def test_clean_canvas_html_detects_canvas_file_links() -> None:
    html = '<p>Baixe a <a href="/courses/48967/files/12345/download">Apostila</a></p>'
    res = clean_canvas_html(html)

    assert 12345 in res.canvas_file_ids


def test_clean_canvas_html_empty_or_none() -> None:
    assert clean_canvas_html(None).text == ""
    assert clean_canvas_html("   ").text == ""
