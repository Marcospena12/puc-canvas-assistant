from __future__ import annotations

import httpx
import pytest
import respx

from canvas_assistant.canvas.client import CanvasClient
from canvas_assistant.canvas.errors import CanvasAuthError, CanvasError

BASE = "https://canvas.test"
TOKEN = "secret-token"


def make_client(http: httpx.AsyncClient, **kwargs) -> CanvasClient:
    return CanvasClient(
        http,
        BASE,
        TOKEN,
        per_page=100,
        retry_base_delay=0,
        rate_limit_pause=0,
        **kwargs,
    )


@respx.mock
async def test_pagination_preserves_next_query() -> None:
    route = respx.get(f"{BASE}/api/v1/courses/1/files").mock(
        side_effect=[
            httpx.Response(
                200,
                json=[{"id": 1}],
                headers={
                    "Link": (f'<{BASE}/api/v1/courses/1/files?page=2&per_page=100>; rel="next"')
                },
            ),
            httpx.Response(200, json=[{"id": 2}]),
        ]
    )

    async with httpx.AsyncClient() as http:
        items = await make_client(http).get_all("/courses/1/files")

    assert [item["id"] for item in items] == [1, 2]
    assert len(route.calls) == 2
    second_params = route.calls[1].request.url.params
    assert second_params["page"] == "2"
    assert second_params["per_page"] == "100"


@respx.mock
async def test_retries_server_error_then_succeeds() -> None:
    route = respx.get(f"{BASE}/api/v1/users/self/profile").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"name": "Aluno"}),
        ]
    )

    async with httpx.AsyncClient() as http:
        data = await make_client(http).get("/users/self/profile")

    assert data == {"name": "Aluno"}
    assert len(route.calls) == 2


@respx.mock
async def test_retries_rate_limit_then_succeeds() -> None:
    route = respx.get(f"{BASE}/api/v1/courses").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=[{"id": 1}]),
        ]
    )

    async with httpx.AsyncClient() as http:
        data = await make_client(http).get_all("/courses")

    assert data == [{"id": 1}]
    assert len(route.calls) == 2


@respx.mock
async def test_auth_error_is_not_retried() -> None:
    route = respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(401, json={"errors": ["unauthorized"]})
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(CanvasAuthError):
            await make_client(http).get_all("/courses")

    assert len(route.calls) == 1


@respx.mock
async def test_get_all_rejects_non_list_payload() -> None:
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(200, json={"not": "a list"})
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(CanvasError):
            await make_client(http).get_all("/courses")


@respx.mock
async def test_client_never_logs_token(caplog) -> None:
    respx.get(f"{BASE}/api/v1/courses").mock(return_value=httpx.Response(200, json=[]))

    async with httpx.AsyncClient() as http:
        await make_client(http).get_all("/courses")

    assert TOKEN not in caplog.text
