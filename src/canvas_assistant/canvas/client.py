from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

import httpx

from .errors import (
    CanvasAuthError,
    CanvasError,
    CanvasForbiddenError,
    CanvasNetworkError,
    CanvasNotFoundError,
    CanvasRateLimitError,
    CanvasServerError,
)

logger = logging.getLogger(__name__)

_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


class CanvasClient:
    """Cliente HTTP somente leitura para a API REST do Canvas."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        token: str,
        *,
        per_page: int = 100,
        timeout: float = 30.0,
        max_retries: int = 4,
        retry_base_delay: float = 1.0,
        rate_limit_pause: float = 2.0,
        max_pages: int = 50,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v1"
        self._token = token
        self.per_page = per_page
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.rate_limit_pause = rate_limit_pause
        self.max_pages = max_pages
        self.last_rate_limit_remaining: int | None = None
        self.last_request_cost: float | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "puc-canvas-assistant/0.1",
        }

    def _absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return self.api_url + ("" if path_or_url.startswith("/") else "/") + path_or_url

    @staticmethod
    def _backoff(attempt: int, base: float) -> float:
        if base <= 0:
            return 0.0
        return base * (2**attempt) + random.uniform(0, base / 2)

    def _track_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-rate-limit-remaining")
        if remaining is not None:
            try:
                self.last_rate_limit_remaining = int(float(remaining))
            except ValueError:
                self.last_rate_limit_remaining = None
        cost = response.headers.get("x-request-cost")
        if cost is not None:
            try:
                self.last_request_cost = float(cost)
            except ValueError:
                self.last_request_cost = None

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = self._absolute(path_or_url)
        attempt = 0
        while True:
            try:
                response = await self.http.request(
                    method,
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    delay = self._backoff(attempt, self.retry_base_delay)
                    logger.warning(
                        "Erro de rede no Canvas (%s); retry %s/%s em %.1fs",
                        type(exc).__name__,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
                raise CanvasNetworkError(f"erro de rede: {exc}", url=url) from exc

            self._track_rate_limit(response)
            status = response.status_code

            if status == 429:
                if attempt < self.max_retries:
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay = max(0.0, float(retry_after)) if retry_after else None
                    except ValueError:
                        delay = None
                    if delay is None:
                        delay = self._backoff(attempt, self.retry_base_delay)
                    logger.warning(
                        "Canvas rate limit (429); retry %s/%s em %.1fs",
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
                raise CanvasRateLimitError(
                    "rate limit do Canvas esgotado",
                    status_code=status,
                    url=url,
                    body=response.text[:500],
                )

            if status >= 500:
                if attempt < self.max_retries:
                    delay = self._backoff(attempt, self.retry_base_delay)
                    logger.warning(
                        "Erro %s no Canvas; retry %s/%s em %.1fs",
                        status,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
                raise CanvasServerError(
                    f"erro {status} no Canvas",
                    status_code=status,
                    url=url,
                    body=response.text[:500],
                )

            if status == 401:
                raise CanvasAuthError(
                    "token do Canvas inválido ou expirado",
                    status_code=status,
                    url=url,
                    body=response.text[:500],
                )
            if status == 403:
                raise CanvasForbiddenError(
                    "sem permissão para este recurso do Canvas",
                    status_code=status,
                    url=url,
                    body=response.text[:500],
                )
            if status == 404:
                raise CanvasNotFoundError(
                    "recurso não encontrado no Canvas",
                    status_code=status,
                    url=url,
                    body=response.text[:500],
                )
            if status >= 400:
                raise CanvasError(
                    f"erro {status} no Canvas",
                    status_code=status,
                    url=url,
                    body=response.text[:500],
                )

            if self.last_rate_limit_remaining is not None:
                if self.last_rate_limit_remaining <= 0:
                    await asyncio.sleep(self.rate_limit_pause)
                elif self.last_rate_limit_remaining < 5:
                    await asyncio.sleep(min(0.5, self.rate_limit_pause / 4))

            return response

    @staticmethod
    def _next_url(response: httpx.Response) -> str | None:
        links = getattr(response, "links", None)
        if links:
            next_link = links.get("next")
            if isinstance(next_link, dict):
                url = next_link.get("url")
                if url:
                    return str(url)
            elif next_link:
                return str(next_link)
        header = response.headers.get("link")
        if not header:
            return None
        for url, rel in _LINK_RE.findall(header):
            if rel == "next":
                return url
        return None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request("GET", path, params=params)
        return response.json()

    async def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        url: str | None = self._absolute(path)
        query = dict(params or {})
        query.setdefault("per_page", self.per_page)
        items: list[Any] = []
        page = 0

        while url:
            response = await self._request("GET", url, params=query or None)
            data = response.json()
            if not isinstance(data, list):
                raise CanvasError(
                    "resposta inesperada do Canvas: esperava uma lista",
                    status_code=response.status_code,
                    url=url,
                )
            items.extend(data)
            page += 1
            if page >= self.max_pages:
                logger.warning("Limite de %s páginas atingido em %s", self.max_pages, path)
                break
            url = self._next_url(response)
            query = {}

        return items
