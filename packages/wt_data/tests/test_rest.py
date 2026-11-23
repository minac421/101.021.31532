import httpx
import pytest

from wt_data.binance.um.rest import (
    BinanceUmRest,
    MinuteWeightBudget,
    RateLimitedError,
    RestError,
    RestrictedLocationError,
    TransientError,
    WindowCountBudget,
)


def test_minute_budget_caps_usage_and_resets_each_minute() -> None:
    budget = MinuteWeightBudget(limit_per_minute=100, fraction=0.5)  # cap = 50
    t = 6000.0  # start of a minute
    assert budget.delay_for(30, t) == 0
    assert budget.delay_for(20, t + 1) == 0
    wait = budget.delay_for(1, t + 2)
    assert wait == pytest.approx(58.05)
    assert budget.delay_for(1, t + 60) == 0  # new window


def test_minute_budget_trusts_the_server_header() -> None:
    budget = MinuteWeightBudget(limit_per_minute=100, fraction=0.5)
    budget.delay_for(1, 6000.0)
    budget.observe(49, 6001.0)  # another process on the same IP used most of the budget
    assert budget.delay_for(5, 6002.0) > 0


def test_window_count_budget() -> None:
    budget = WindowCountBudget(limit=2, window_s=10)
    assert budget.delay_for(0.0) == 0
    assert budget.delay_for(1.0) == 0
    assert budget.delay_for(2.0) == pytest.approx(8.01)
    assert budget.delay_for(10.5) == 0


def _client(handler: httpx.MockTransport) -> BinanceUmRest:
    return BinanceUmRest("https://fapi.example", transport=handler)


async def test_successful_request_records_weight_and_origin() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/depth"
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(
            200, content=b'{"lastUpdateId":1,"bids":[],"asks":[]}', headers={"X-MBX-USED-WEIGHT-1M": "42"}
        )

    async with _client(httpx.MockTransport(handler)) as rest:
        response = await rest.depth("BTCUSDT", 1000)
    assert response.used_weight_1m == 42
    assert response.origin == "GET:/fapi/v1/depth?limit=1000&symbol=BTCUSDT"
    assert response.latency_ms >= 0


@pytest.mark.parametrize("status", [418, 429])
async def test_rate_limit_pauses_all_requests(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Retry-After": "120"})

    async with _client(httpx.MockTransport(handler)) as rest:
        with pytest.raises(RateLimitedError) as info:
            await rest.server_time()
        assert info.value.retry_after_s == 120
        assert rest.blocked_for_s > 100


async def test_server_and_client_errors_are_typed() -> None:
    responses = iter([httpx.Response(503), httpx.Response(400, content=b'{"code":-1121}')])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with _client(httpx.MockTransport(handler)) as rest:
        with pytest.raises(TransientError):
            await rest.exchange_info()
        with pytest.raises(RestError) as info:
            await rest.exchange_info()
        assert info.value.status == 400


async def test_transport_failure_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _client(httpx.MockTransport(handler)) as rest:
        with pytest.raises(TransientError):
            await rest.open_interest("BTCUSDT")


async def test_unsupported_depth_limit() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200))) as rest:
        with pytest.raises(ValueError, match="depth limit"):
            await rest.depth("BTCUSDT", 7)


async def test_restricted_location_is_fatal_not_transient() -> None:
    body = b'{"code":0,"msg":"Service unavailable from a restricted location"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(451, content=body)

    async with _client(httpx.MockTransport(handler)) as rest:
        with pytest.raises(RestrictedLocationError, match="eligible jurisdiction") as info:
            await rest.exchange_info()
    assert info.value.status == 451
    assert not isinstance(info.value, TransientError | RateLimitedError)
