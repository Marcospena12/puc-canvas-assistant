from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class FileTooLargeError(DownloadError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadResult:
    sha256: str
    size: int
    path: Path


class FileDownloader:
    """Baixa arquivos seguindo redirecionamentos sem repassar Authorization para terceiros."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        canvas_base_url: str,
        token: str,
        *,
        timeout: float = 120.0,
        max_redirects: int = 5,
        user_agent: str = "puc-canvas-assistant/0.1",
    ) -> None:
        self.http = http
        self.canvas_base_url = canvas_base_url.rstrip("/")
        self.canvas_netloc = urlparse(self.canvas_base_url).netloc.casefold()
        self._token = token
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = user_agent

    def _headers(self, url: str) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        netloc = urlparse(url).netloc.casefold()
        if netloc == self.canvas_netloc:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        max_bytes: int,
        expected_size: int | None = None,
    ) -> DownloadResult:
        dest.parent.mkdir(parents=True, exist_ok=True)
        current = url

        for _ in range(self.max_redirects + 1):
            headers = self._headers(current)
            try:
                response = await self.http.send(
                    self.http.build_request("GET", current, headers=headers),
                    stream=True,
                )
            except httpx.HTTPError as exc:
                raise DownloadError(f"erro de rede ao baixar arquivo: {exc}") from exc

            status = response.status_code

            if status in (301, 302, 303, 307, 308):
                await response.aclose()
                location = response.headers.get("location")
                if not location:
                    raise DownloadError(
                        f"resposta de redirect {status} sem header Location",
                        status_code=status,
                        retryable=False,
                    )
                current = urljoin(current, location)
                continue

            if status != 200:
                body = (await response.aread())[:300].decode("utf-8", errors="replace")
                await response.aclose()
                retryable = status >= 500 or status in (401, 403, 429)
                raise DownloadError(
                    f"falha no download (HTTP {status}): {body}",
                    status_code=status,
                    retryable=retryable,
                )

            length_header = response.headers.get("content-length")
            if length_header:
                try:
                    if int(length_header) > max_bytes:
                        await response.aclose()
                        raise FileTooLargeError(
                            f"tamanho anunciado ({length_header} B) excede limite de {max_bytes} B",
                            status_code=200,
                            retryable=False,
                        )
                except ValueError:
                    pass

            fd, temp_path_str = tempfile.mkstemp(
                prefix=f"{dest.name}.",
                suffix=".part",
                dir=dest.parent,
            )
            temp_path = Path(temp_path_str)
            hasher = hashlib.sha256()
            total = 0

            try:
                with os.fdopen(fd, "wb") as handle:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise FileTooLargeError(
                                f"arquivo excedeu limite de {max_bytes} B durante download",
                                status_code=200,
                                retryable=False,
                            )
                        hasher.update(chunk)
                        handle.write(chunk)
            except BaseException:
                await response.aclose()
                temp_path.unlink(missing_ok=True)
                raise
            finally:
                await response.aclose()

            if expected_size is not None and total != expected_size:
                logger.warning(
                    "Tamanho baixado (%s B) difere do esperado (%s B) para %s",
                    total,
                    expected_size,
                    dest.name,
                )

            os.replace(temp_path, dest)
            return DownloadResult(sha256=hasher.hexdigest(), size=total, path=dest)

        raise DownloadError(
            f"limite de {self.max_redirects} redirecionamentos atingido",
            retryable=False,
        )
