from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


BASE_USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DISCOVERY_PROTOCOL_VERSION = 2
RESOURCE_PROTOCOL_VERSION = 2

_NETWORK_LABELS = {
    "eip155:8453": "Base",
    "eip155:84532": "Base Sepolia",
    "eip155:137": "Polygon",
    "eip155:42161": "Arbitrum",
    "eip155:10": "Optimism",
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp": "Solana Mainnet",
    "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1": "Solana Devnet",
}


def _clean_url(url: str) -> str:
    return url.rstrip("/")


def usd_to_raw_usdc(price_usd: str) -> str:
    amount = Decimal(price_usd.lstrip("$"))
    return str(int(amount * Decimal("1000000")))


def network_label(network: str) -> str:
    return _NETWORK_LABELS.get(network, network)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class MerchantResource:
    path: str
    method: str
    description: str
    price_usd: str
    network: str = "eip155:8453"
    mime_type: str = "application/json"
    resource_type: str = "http"
    metadata: dict[str, Any] = field(default_factory=dict)

    def resource_url(self, origin: str) -> str:
        return f"{_clean_url(origin)}{self.path}"


@dataclass(frozen=True)
class FacilitatorMetadata:
    name: str
    public_url: str
    seller_origin: str
    version: str
    protocol: str = "x402"
    discovery_version: int = DISCOVERY_PROTOCOL_VERSION
    resource_version: int = RESOURCE_PROTOCOL_VERSION
    usdc_asset: str = BASE_USDC_ASSET
    merchant_name: str = "Satoshi API"
    merchant_description: str = "Bitcoin fee intelligence API for AI agents"
    resources: list[MerchantResource] = field(default_factory=list)
    endpoint_map: dict[str, str] = field(
        default_factory=lambda: {
            "verify": "POST /verify",
            "settle": "POST /settle",
            "supported": "GET /supported",
            "discovery": "GET /discovery/resources",
            "merchantDiscovery": "GET /discovery/merchant",
            "settlements": "GET /settlements/{payTo}",
            "stats": "GET /settlements/{payTo}/stats",
            "observability": "GET /observability/summary",
            "health": "GET /health",
        }
    )

    def merchant_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "name": self.merchant_name,
                "url": self.seller_origin,
                "description": self.merchant_description,
                "paid_endpoints": len(self.resources),
            }
        ]

    def build_discovery_item(self, resource: MerchantResource, pay_to: str) -> dict[str, Any]:
        resource_url = resource.resource_url(self.seller_origin)
        input_schema = {
            "type": resource.resource_type,
            "method": resource.method,
            "resource": resource_url,
            **dict(resource.metadata.get("input", {})),
        }
        output_schema = {
            "mimeType": resource.mime_type,
            **dict(resource.metadata.get("output", {})),
        }
        metadata = dict(resource.metadata)
        metadata.setdefault("description", resource.description)
        metadata.setdefault("input", input_schema)
        metadata.setdefault("output", output_schema)
        metadata.setdefault("extensions", ["bazaar"])
        metadata.setdefault("network", resource.network)
        metadata.setdefault("priceUsd", resource.price_usd)
        amount_raw = usd_to_raw_usdc(resource.price_usd)
        return {
            "resource": resource_url,
            "type": resource.resource_type,
            "x402Version": self.resource_version,
            "description": resource.description,
            "mimeType": resource.mime_type,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": resource.network,
                    "amount": amount_raw,
                    "maxAmountRequired": amount_raw,
                    "asset": self.usdc_asset,
                    "payTo": pay_to,
                    "maxTimeoutSeconds": 300,
                    "extra": {
                        "name": "USD Coin",
                        "version": str(self.resource_version),
                        "facilitatorUrl": self.public_url,
                    },
                    "outputSchema": {
                        "input": input_schema,
                        "output": output_schema,
                    },
                }
            ],
            "discoveryInfo": {
                "input": input_schema,
                "output": output_schema,
            },
            "lastUpdated": _iso_now(),
            "metadata": metadata,
        }


def default_facilitator_metadata() -> FacilitatorMetadata:
    return FacilitatorMetadata(
        name="Satoshi x402 Facilitator",
        public_url=_clean_url(os.environ.get("FACILITATOR_PUBLIC_URL", "https://facilitator.bitcoinsapi.com")),
        seller_origin=_clean_url(os.environ.get("SELLER_ORIGIN", "https://bitcoinsapi.com")),
        version=os.environ.get("FACILITATOR_VERSION", "1.7.0"),
        resources=[
            MerchantResource(
                path="/api/v1/ai/explain-tx",
                method="POST",
                description="AI-powered Bitcoin transaction explainer",
                price_usd="0.01",
            ),
            MerchantResource(
                path="/api/v1/ai/explain-block",
                method="POST",
                description="AI-powered Bitcoin block analysis",
                price_usd="0.01",
            ),
            MerchantResource(
                path="/api/v1/ai/fee-advice",
                method="GET",
                description="AI fee optimization recommendation",
                price_usd="0.01",
            ),
            MerchantResource(
                path="/api/v1/ai/chat",
                method="POST",
                description="Bitcoin knowledge chatbot",
                price_usd="0.01",
            ),
            MerchantResource(
                path="/api/v1/broadcast",
                method="POST",
                description="Broadcast a signed Bitcoin transaction",
                price_usd="0.01",
            ),
            MerchantResource(
                path="/api/v1/mining/nextblock",
                method="GET",
                description="Next block prediction with fee analysis",
                price_usd="0.01",
            ),
            MerchantResource(
                path="/api/v1/fees/observatory/scoreboard",
                method="GET",
                description="Fee estimator accuracy scoreboard",
                price_usd="0.005",
            ),
            MerchantResource(
                path="/api/v1/fees/observatory/block-stats",
                method="GET",
                description="Per-block fee statistics",
                price_usd="0.005",
            ),
            MerchantResource(
                path="/api/v1/fees/observatory/estimates",
                method="GET",
                description="Fee estimate history from multiple sources",
                price_usd="0.005",
            ),
            MerchantResource(
                path="/api/v1/fees/landscape",
                method="GET",
                description="Complete fee landscape analysis",
                price_usd="0.005",
            ),
        ],
    )
