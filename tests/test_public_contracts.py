from pathlib import Path

from fastapi.testclient import TestClient

from app_factory import create_app
from facilitator_metadata import FacilitatorMetadata, MerchantResource
from facilitator_runtime import FacilitatorRuntimeState
from facilitator_store import FacilitatorStore


class StubFacilitator:
    def __init__(self) -> None:
        self.verify_calls: list[tuple[dict, dict]] = []
        self.settle_calls: list[tuple[dict, dict]] = []

    async def verify_request(self, payment_payload: dict, payment_requirements: dict) -> dict:
        self.verify_calls.append((payment_payload, payment_requirements))
        return {"isValid": True, "payer": "0xpayer"}

    async def settle_request(self, payment_payload: dict, payment_requirements: dict) -> dict:
        self.settle_calls.append((payment_payload, payment_requirements))
        return {
            "success": True,
            "transaction": "0xsettled",
            "network": payment_requirements["network"],
        }


def make_runtime() -> FacilitatorRuntimeState:
    metadata = FacilitatorMetadata(
        name="Satoshi x402 Facilitator",
        public_url="https://facilitator.bitcoinsapi.com",
        seller_origin="https://bitcoinsapi.com",
        version="1.6.1",
        resources=[
            MerchantResource(
                path="/api/v1/ai/chat",
                method="POST",
                description="Bitcoin knowledge chatbot",
                price_usd="0.01",
                network="eip155:8453",
            ),
            MerchantResource(
                path="/api/v1/fees/landscape",
                method="GET",
                description="Complete fee landscape analysis",
                price_usd="0.005",
                network="eip155:8453",
            ),
            MerchantResource(
                path="/api/v1/fees/observatory/scoreboard",
                method="GET",
                description="Fee estimator accuracy scoreboard",
                price_usd="0.005",
                network="eip155:137",
            ),
        ],
    )
    return FacilitatorRuntimeState(
        facilitator=StubFacilitator(),
        metadata=metadata,
        supported_kinds=[
            {"x402Version": 2, "scheme": "exact", "network": "eip155:8453", "extra": {}},
            {"x402Version": 2, "scheme": "exact", "network": "eip155:84532", "extra": {}},
        ],
        extensions=["bazaar", "eip2612GasSponsoring"],
        signers={"eip155": ["0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"]},
        pay_to_by_network={
            "eip155:8453": "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01",
            "eip155:84532": "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01",
        },
    )


def make_client(tmp_path: Path) -> TestClient:
    runtime = make_runtime()
    store = FacilitatorStore(tmp_path / "settlements.db")
    return TestClient(create_app(runtime=runtime, store=store))


def test_supported_only_returns_live_networks(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/supported")

    assert response.status_code == 200
    body = response.json()
    assert body["extensions"] == ["bazaar", "eip2612GasSponsoring"]
    assert {kind["network"] for kind in body["kinds"]} == {"eip155:8453", "eip155:84532"}
    assert all(kind["scheme"] == "exact" for kind in body["kinds"])


def test_discovery_returns_bazaar_shape_with_live_resources_only(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/discovery/resources", params={"limit": 1, "offset": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["x402Version"] == 2
    assert body["pagination"] == {"limit": 1, "offset": 1, "total": 2}
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["resource"] == "https://bitcoinsapi.com/api/v1/fees/landscape"
    assert item["type"] == "http"
    assert item["metadata"]["description"] == "Complete fee landscape analysis"
    assert item["metadata"]["input"]["method"] == "GET"
    assert item["metadata"]["output"]["mimeType"] == "application/json"
    assert item["accepts"][0]["network"] == "eip155:8453"
    assert item["accepts"][0]["outputSchema"]["input"]["method"] == "GET"
    assert item["accepts"][0]["outputSchema"]["output"]["mimeType"] == "application/json"
    assert item["accepts"][0]["extra"]["facilitatorUrl"] == "https://facilitator.bitcoinsapi.com"


def test_llms_and_health_stay_in_sync_with_supported(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    supported = client.get("/supported").json()
    llms = client.get("/llms.txt").json()
    health = client.get("/health").json()

    assert health["version"] == "1.6.1"
    assert llms["version"] == "1.6.1"
    assert llms["url"] == "https://facilitator.bitcoinsapi.com"
    assert set(llms["supported_networks"]) == {kind["network"] for kind in supported["kinds"]}
    assert llms["extensions"] == supported["extensions"]


def test_verify_and_settle_update_observability_summary(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    payment_request = {
        "paymentPayload": {"signature": "demo"},
        "paymentRequirements": {
            "scheme": "exact",
            "network": "eip155:8453",
            "resource": "https://bitcoinsapi.com/api/v1/ai/chat",
            "payTo": "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01",
            "maxAmountRequired": "10000",
        },
    }

    verify_response = client.post("/verify", json=payment_request)
    settle_response = client.post("/settle", json=payment_request)
    summary = client.get("/observability/summary")

    assert verify_response.status_code == 200
    assert settle_response.status_code == 200
    assert summary.status_code == 200
    body = summary.json()
    assert body["events"]["verify_attempt"] == 1
    assert body["events"]["verify_success"] == 1
    assert body["events"]["settle_attempt"] == 1
    assert body["events"]["settle_success"] == 1
    assert body["resources"][0]["resource"] == "https://bitcoinsapi.com/api/v1/ai/chat"
    assert body["resources"][0]["settlements"] == 1


def test_status_exposes_live_capabilities_and_observability(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    payment_request = {
        "paymentPayload": {"signature": "demo"},
        "paymentRequirements": {
            "scheme": "exact",
            "network": "eip155:8453",
            "resource": "https://bitcoinsapi.com/api/v1/fees/landscape",
            "payTo": "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01",
            "maxAmountRequired": "5000",
        },
    }

    client.post("/verify", json=payment_request)
    client.post("/settle", json=payment_request)

    response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.6.1"
    assert body["url"] == "https://facilitator.bitcoinsapi.com"
    assert body["supported"]["kinds"][0]["network"] == "eip155:8453"
    assert "bazaar" in body["supported"]["extensions"]
    assert body["observability"]["events"]["verify_attempt"] == 1
    assert body["observability"]["resources"][0]["resource"] == "https://bitcoinsapi.com/api/v1/fees/landscape"
    assert body["observability"]["resources"][0]["settlements"] == 1


def test_discovery_supports_query_filters_and_merchant_lookup(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    filtered = client.get(
        "/discovery/resources",
        params={
            "query": "fee landscape",
            "network": "eip155:8453",
            "extensions": "bazaar",
            "maxUsdPrice": "0.005",
        },
    )
    merchant = client.get(
        "/discovery/merchant",
        params={
            "payTo": "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01",
            "limit": 10,
            "offset": 0,
        },
    )

    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body["pagination"]["total"] == 1
    assert filtered_body["items"][0]["resource"] == "https://bitcoinsapi.com/api/v1/fees/landscape"

    assert merchant.status_code == 200
    merchant_body = merchant.json()
    assert merchant_body["payTo"] == "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"
    assert merchant_body["pagination"] == {"limit": 10, "offset": 0, "total": 2}
    assert {item["resource"] for item in merchant_body["resources"]} == {
        "https://bitcoinsapi.com/api/v1/ai/chat",
        "https://bitcoinsapi.com/api/v1/fees/landscape",
    }
