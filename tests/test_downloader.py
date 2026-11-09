from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
import respx

from canvas_assistant.knowledge.downloader import (
    DownloadError,
    FileDownloader,
    FileTooLargeError,
)

CANVAS_BASE = "https://canvas.test"
TOKEN = "secret-token"


def make_downloader(http: httpx.AsyncClient, **kwargs) -> FileDownloader:
    return FileDownloader(http, CANVAS_BASE, TOKEN, timeout=10.0, **kwargs)


@respx.mock
async def test_downloader_strips_authorization_on_third_party_redirect(tmp_path: Path) -> None:
    canvas_url = f"{CANVAS_BASE}/files/1/download"
    cdn_url = "https://storage.thirdparty.com/blobs/aula.pdf"
    content = b"conteudo de teste do arquivo pdf"

    route_canvas = respx.get(canvas_url).mock(
        return_value=httpx.Response(302, headers={"Location": cdn_url})
    )
    route_cdn = respx.get(cdn_url).mock(
        return_value=httpx.Response(
            200, content=content, headers={"Content-Length": str(len(content))}
        )
    )

    dest = tmp_path / "aula.pdf"
    async with httpx.AsyncClient() as http:
        downloader = make_downloader(http)
        result = await downloader.download(canvas_url, dest, max_bytes=1024)

    assert result.size == len(content)
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert dest.read_bytes() == content

    assert len(route_canvas.calls) == 1
    assert route_canvas.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"

    assert len(route_cdn.calls) == 1
    assert "authorization" not in route_cdn.calls[0].request.headers


@respx.mock
async def test_downloader_keeps_authorization_on_same_origin_redirect(tmp_path: Path) -> None:
    first_url = f"{CANVAS_BASE}/files/1/download"
    second_url = f"{CANVAS_BASE}/files/1/final_download"
    content = b"arquivo interno"

    route_first = respx.get(first_url).mock(
        return_value=httpx.Response(302, headers={"Location": second_url})
    )
    route_second = respx.get(second_url).mock(return_value=httpx.Response(200, content=content))

    dest = tmp_path / "interno.pdf"
    async with httpx.AsyncClient() as http:
        downloader = make_downloader(http)
        result = await downloader.download(first_url, dest, max_bytes=1024)

    assert result.size == len(content)
    assert route_first.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
    assert route_second.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"


@respx.mock
async def test_downloader_rejects_file_exceeding_max_bytes_cleanly(tmp_path: Path) -> None:
    url = f"{CANVAS_BASE}/files/2/download"
    large_content = b"x" * 500

    respx.get(url).mock(return_value=httpx.Response(200, content=large_content))

    dest = tmp_path / "grande.pdf"
    async with httpx.AsyncClient() as http:
        downloader = make_downloader(http)
        with pytest.raises(FileTooLargeError):
            await downloader.download(url, dest, max_bytes=100)

    assert not dest.exists()
    leftovers = list(tmp_path.glob("*.part"))
    assert leftovers == []


@respx.mock
async def test_downloader_fails_on_http_error(tmp_path: Path) -> None:
    url = f"{CANVAS_BASE}/files/3/download"
    respx.get(url).mock(return_value=httpx.Response(404, text="not found"))

    dest = tmp_path / "inexistente.pdf"
    async with httpx.AsyncClient() as http:
        downloader = make_downloader(http)
        with pytest.raises(DownloadError) as exc:
            await downloader.download(url, dest, max_bytes=1024)

    assert exc.value.status_code == 404
    assert not dest.exists()
