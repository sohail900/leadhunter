import asyncio

import httpx
import structlog

log = structlog.get_logger()

USER_AGENT = "LeadHunter/0.1 (personal job alert reader)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_DELAY = 60.0


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


def _retry_delay(response: httpx.Response | None, attempt: int, backoff: float) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(float(retry_after), MAX_RETRY_DELAY)
    return min(backoff * 2**attempt, MAX_RETRY_DELAY)


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    retries: int = 3,
    backoff: float = 2.0,
) -> httpx.Response:
    for attempt in range(retries + 1):
        response = None
        try:
            response = await client.get(url, params=params)
            if response.status_code not in RETRY_STATUSES:
                response.raise_for_status()
                return response
            error: Exception = httpx.HTTPStatusError(
                f"HTTP {response.status_code}", request=response.request, response=response
            )
        except httpx.TransportError as exc:
            error = exc

        if attempt == retries:
            raise error
        delay = _retry_delay(response, attempt, backoff)
        log.warning("http_retry", url=url, attempt=attempt + 1, delay=delay, error=str(error))
        await asyncio.sleep(delay)

    raise AssertionError("unreachable")
