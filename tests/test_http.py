import httpx
import pytest

from leadhunter import http
from leadhunter.http import get_with_retry


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(http.asyncio, "sleep", fake_sleep)
    return delays


def client_returning(responses):
    queue = list(responses)

    def handler(request):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_retries_server_errors_with_exponential_backoff(no_sleep):
    client = client_returning([httpx.Response(503), httpx.Response(502), httpx.Response(200)])

    response = await get_with_retry(client, "https://example.test/feed")

    assert response.status_code == 200
    assert no_sleep == [2.0, 4.0]


async def test_rate_limit_waits_for_retry_after(no_sleep):
    client = client_returning(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )

    await get_with_retry(client, "https://example.test/feed")

    assert no_sleep == [7.0]


async def test_network_errors_are_retried(no_sleep):
    client = client_returning([httpx.ConnectError("boom"), httpx.Response(200)])

    response = await get_with_retry(client, "https://example.test/feed")

    assert response.status_code == 200


async def test_gives_up_after_the_last_retry(no_sleep):
    client = client_returning([httpx.Response(503)] * 3)

    with pytest.raises(httpx.HTTPStatusError):
        await get_with_retry(client, "https://example.test/feed", retries=2)

    assert len(no_sleep) == 2


async def test_client_errors_are_not_retried(no_sleep):
    client = client_returning([httpx.Response(404)])

    with pytest.raises(httpx.HTTPStatusError):
        await get_with_retry(client, "https://example.test/feed")

    assert no_sleep == []
