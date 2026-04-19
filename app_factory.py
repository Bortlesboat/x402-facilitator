from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from facilitator_runtime import FacilitatorRuntimeState, build_runtime_from_env
from facilitator_store import FacilitatorStore


API_KEYS_FILE = os.environ.get("API_KEYS_FILE", "api_keys.json")


class PaymentRequest(BaseModel):
    paymentPayload: dict
    paymentRequirements: dict


def _load_api_keys() -> dict:
    if os.path.exists(API_KEYS_FILE):
        with open(API_KEYS_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def _etag_for(payload: dict) -> str:
    return hashlib.md5(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def create_app(
    *,
    runtime: FacilitatorRuntimeState | None = None,
    store: FacilitatorStore | None = None,
) -> FastAPI:
    runtime = runtime or build_runtime_from_env()
    store = store or FacilitatorStore(Path(os.environ.get("SETTLEMENT_DB", "settlements.db")))
    metadata = runtime.metadata

    app = FastAPI(
        title=metadata.name,
        description="x402 payment facilitator for Satoshi API with Bazaar-compatible discovery.",
        version=metadata.version,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.runtime = runtime
    app.state.store = store

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if "server" in response.headers:
            del response.headers["server"]
        response.headers["Server"] = "Satoshi"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post("/verify")
    async def verify(request: PaymentRequest):
        requirements = request.paymentRequirements
        resource = requirements.get("resource", "")
        network = requirements.get("network", "")
        pay_to = requirements.get("payTo", "")
        store.record_event("verify_attempt", resource=resource, network=network, pay_to=pay_to, status="pending")
        try:
            result = await runtime.verify_request(request.paymentPayload, request.paymentRequirements)
        except Exception as exc:
            store.record_event(
                "verify_failure",
                resource=resource,
                network=network,
                pay_to=pay_to,
                status="error",
                detail=str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        event_type = "verify_success" if result.get("isValid") else "verify_failure"
        store.record_event(
            event_type,
            resource=resource,
            network=network,
            pay_to=pay_to,
            status="ok" if result.get("isValid") else "rejected",
            detail=result.get("invalidReason", ""),
        )
        return result

    @app.post("/settle")
    async def settle(request: PaymentRequest):
        requirements = request.paymentRequirements
        resource = requirements.get("resource", "")
        network = requirements.get("network", "")
        pay_to = requirements.get("payTo", "")
        amount = str(requirements.get("maxAmountRequired", requirements.get("amount", "")))
        store.record_event("settle_attempt", resource=resource, network=network, pay_to=pay_to, status="pending")
        try:
            result = await runtime.settle_request(request.paymentPayload, request.paymentRequirements)
        except Exception as exc:
            store.record_event(
                "settle_failure",
                resource=resource,
                network=network,
                pay_to=pay_to,
                status="error",
                detail=str(exc),
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        event_type = "settle_success" if result.get("success") else "settle_failure"
        store.record_event(
            event_type,
            resource=resource,
            network=network,
            pay_to=pay_to,
            status="ok" if result.get("success") else "failed",
            detail=result.get("errorReason", ""),
        )
        if result.get("success") and result.get("transaction"):
            store.record_settlement(
                result["transaction"],
                pay_to,
                amount,
                network,
                resource=resource,
            )
        return result

    @app.get("/supported")
    async def supported(request: Request):
        payload = runtime.supported_payload()
        etag = _etag_for(payload)
        if request.headers.get("if-none-match", "").strip('"') == etag:
            return JSONResponse(status_code=304, content=None)
        return JSONResponse(
            content=payload,
            headers={"ETag": f'"{etag}"', "Cache-Control": "public, max-age=300"},
        )

    @app.get("/discovery/resources")
    async def discovery(
        limit: int = Query(default=100, ge=0, le=200),
        offset: int = Query(default=0, ge=0),
        resource_type: str | None = Query(default=None, alias="type"),
        query: str | None = Query(default=None),
        network: str | None = Query(default=None),
        asset: str | None = Query(default=None),
        scheme: str | None = Query(default=None),
        pay_to: str | None = Query(default=None, alias="payTo"),
        max_usd_price: str | None = Query(default=None, alias="maxUsdPrice"),
        extensions: str | None = Query(default=None),
    ):
        return runtime.discovery_payload(
            limit=limit,
            offset=offset,
            resource_type=resource_type,
            query=query,
            network=network,
            asset=asset,
            scheme=scheme,
            pay_to=pay_to,
            max_usd_price=max_usd_price,
            extensions=extensions,
        )

    @app.get("/discovery/merchant")
    async def discovery_merchant(
        pay_to: str = Query(alias="payTo"),
        limit: int = Query(default=25, ge=0, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        return runtime.merchant_payload(pay_to=pay_to, limit=limit, offset=offset)

    @app.get("/llms.txt", response_class=JSONResponse)
    async def llms_txt():
        return JSONResponse(content=runtime.llms_payload(), media_type="application/json")

    @app.get("/health")
    async def health():
        return runtime.health_payload()

    @app.get("/status")
    async def status():
        return runtime.status_payload(observability=store.get_observability_summary())

    @app.get("/settlements/{pay_to}")
    async def get_settlements(
        pay_to: str,
        limit: int = Query(default=50, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        return store.get_settlements(pay_to, limit, offset)

    @app.get("/settlements/{pay_to}/stats")
    async def get_settlement_stats(pay_to: str):
        return store.get_settlement_stats(pay_to)

    @app.get("/observability/summary")
    async def observability_summary():
        return store.get_observability_summary()

    @app.post("/verify-key")
    async def verify_api_key(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        key = auth[7:]
        keys = _load_api_keys()
        if key not in keys:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {
            "valid": True,
            "merchant": keys[key].get("merchant", ""),
            "note": keys[key].get("note", ""),
        }

    return app
