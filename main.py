"""Satoshi x402 Facilitator.

Multi-chain facilitator: Base, Polygon, Arbitrum, Optimism, Solana + testnets.
Gas sponsoring + bazaar extensions. x402 v1 + v2 support.

Run with: uvicorn main:app --host 0.0.0.0 --port 4022
"""

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from x402 import x402Facilitator
from x402.mechanisms.evm import FacilitatorWeb3Signer
from x402.mechanisms.evm.exact.facilitator import ExactEvmScheme, ExactEvmSchemeConfig
from x402.extensions.eip2612_gas_sponsoring import EIP2612_GAS_SPONSORING
from x402.extensions.erc20_approval_gas_sponsoring import (
    Erc20ApprovalFacilitatorExtension,
    WriteContractCall,
)
from x402.extensions.bazaar import BAZAAR

load_dotenv()

PORT = int(os.environ.get("PORT", "4022"))

# --- Settlement history (SQLite) ---
DB_PATH = Path(os.environ.get("SETTLEMENT_DB", "settlements.db"))

def _init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_hash TEXT NOT NULL,
            pay_to TEXT NOT NULL,
            amount TEXT NOT NULL,
            network TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'settled',
            created_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_to ON settlements(pay_to)")
    conn.commit()
    conn.close()

_init_db()

def record_settlement(tx_hash: str, pay_to: str, amount: str, network: str, status: str = "settled"):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO settlements (tx_hash, pay_to, amount, network, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (tx_hash, pay_to, amount, network, status, time.time()),
    )
    conn.commit()
    conn.close()

# --- API key bypass ---
# Keys file: JSON mapping key -> {"merchant": "0x...", "note": "..."}
API_KEYS_FILE = os.environ.get("API_KEYS_FILE", "api_keys.json")

def _load_api_keys() -> dict:
    if os.path.exists(API_KEYS_FILE):
        with open(API_KEYS_FILE) as f:
            return json.load(f)
    return {}

evm_private_key = os.environ.get("EVM_PRIVATE_KEY")
svm_private_key = os.environ.get("SVM_PRIVATE_KEY")

if not evm_private_key and not svm_private_key:
    print("At least one of EVM_PRIVATE_KEY or SVM_PRIVATE_KEY is required")
    sys.exit(1)

facilitator = x402Facilitator()

# Base mainnet + Sepolia testnet (EVM)
if evm_private_key:
    evm_mainnet_signer = FacilitatorWeb3Signer(
        private_key=evm_private_key,
        rpc_url=os.environ.get("EVM_RPC_URL", "https://mainnet.base.org"),
    )
    config = ExactEvmSchemeConfig(deploy_erc4337_with_eip6492=True)
    facilitator.register(["eip155:8453"], ExactEvmScheme(evm_mainnet_signer, config))
    print(f"EVM mainnet address: {evm_mainnet_signer.get_addresses()[0]}")

    # Gas sponsoring extensions (facilitator pays ~$0.0001/tx gas on Base)
    facilitator.register_extension(EIP2612_GAS_SPONSORING)

    class Web3BatchSigner:
        """Wraps FacilitatorWeb3Signer for batched approval+settle."""

        def __init__(self, w3_signer):
            self._signer = w3_signer

        def send_transactions(self, transactions):
            hashes = []
            for tx in transactions:
                if isinstance(tx, str):
                    tx_hash = self._signer.w3.eth.send_raw_transaction(tx).hex()
                    self._signer.wait_for_transaction_receipt(tx_hash)
                    hashes.append(tx_hash)
                elif isinstance(tx, WriteContractCall):
                    tx_hash = self._signer.write_contract(
                        tx.address, tx.abi, tx.function, *tx.args
                    )
                    hashes.append(tx_hash)
            return hashes

        def wait_for_transaction_receipt(self, tx_hash):
            return self._signer.wait_for_transaction_receipt(tx_hash)

    erc20_ext = Erc20ApprovalFacilitatorExtension(signer=Web3BatchSigner(evm_mainnet_signer))
    facilitator.register_extension(erc20_ext)
    facilitator.register_extension(BAZAAR)
    print("Extensions: eip2612GasSponsoring, erc20ApprovalGasSponsoring, bazaar")

    # Additional EVM mainnets — only register if env var explicitly enables them.
    # Each chain needs funded gas (ETH) in the facilitator wallet to settle.
    # Set ENABLE_POLYGON=1, ENABLE_ARBITRUM=1, ENABLE_OPTIMISM=1 to activate.
    extra_chains = {
        "eip155:137": ("Polygon", "POLYGON_RPC_URL", "https://polygon.llamarpc.com", "ENABLE_POLYGON"),
        "eip155:42161": ("Arbitrum", "ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc", "ENABLE_ARBITRUM"),
        "eip155:10": ("Optimism", "OPTIMISM_RPC_URL", "https://mainnet.optimism.io", "ENABLE_OPTIMISM"),
    }
    for chain_id, (name, rpc_env, rpc_default, enable_env) in extra_chains.items():
        if os.environ.get(enable_env):
            try:
                signer = FacilitatorWeb3Signer(private_key=evm_private_key, rpc_url=os.environ.get(rpc_env, rpc_default))
                facilitator.register([chain_id], ExactEvmScheme(signer, config))
                print(f"{name} ({chain_id}) registered")
            except Exception as e:
                print(f"{name} ({chain_id}) failed: {e}")
        else:
            print(f"{name} ({chain_id}) skipped — set {enable_env}=1 and fund gas to enable")

    # Base Sepolia testnet (same key, different RPC)
    evm_testnet_signer = FacilitatorWeb3Signer(
        private_key=evm_private_key,
        rpc_url=os.environ.get("EVM_TESTNET_RPC_URL", "https://sepolia.base.org"),
    )
    facilitator.register(["eip155:84532"], ExactEvmScheme(evm_testnet_signer, config))
    print(f"EVM testnet address: {evm_testnet_signer.get_addresses()[0]}")

# Solana mainnet + devnet
if svm_private_key:
    from solders.keypair import Keypair
    from x402.mechanisms.svm import FacilitatorKeypairSigner
    from x402.mechanisms.svm.exact.facilitator import ExactSvmScheme

    svm_keypair = Keypair.from_base58_string(svm_private_key)
    svm_signer = FacilitatorKeypairSigner(svm_keypair)
    facilitator.register(["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"], ExactSvmScheme(svm_signer))
    print(f"SVM mainnet address: {svm_signer.get_addresses()[0]}")

    # Solana devnet (same keypair, different network ID — SDK handles RPC routing)
    facilitator.register(["solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"], ExactSvmScheme(svm_signer))
    print("SVM devnet registered")


class PaymentRequest(BaseModel):
    paymentPayload: dict
    paymentRequirements: dict


app = FastAPI(
    title="Satoshi x402 Facilitator",
    description="Multi-chain x402 facilitator: Base, Polygon, Arbitrum, Optimism, Solana + testnets. Gas sponsoring + bazaar.",
    version="1.6.0",
    docs_url="/docs",
    redoc_url=None,
)

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
    try:
        from x402.schemas import PaymentRequirements, parse_payment_payload

        payload = parse_payment_payload(request.paymentPayload)
        requirements = PaymentRequirements.model_validate(request.paymentRequirements)
        response = await facilitator.verify(payload, requirements)
        return response.model_dump(by_alias=True, exclude_none=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/settle")
async def settle(request: PaymentRequest):
    try:
        from x402.schemas import PaymentRequirements, parse_payment_payload

        payload = parse_payment_payload(request.paymentPayload)
        requirements = PaymentRequirements.model_validate(request.paymentRequirements)
        response = await facilitator.settle(payload, requirements)
        result = response.model_dump(by_alias=True, exclude_none=True)

        # Record settlement for history
        tx_hash = result.get("transaction", "") or result.get("txHash", "")
        pay_to = request.paymentRequirements.get("payTo", "")
        amount = str(request.paymentRequirements.get("maxAmountRequired", ""))
        network = request.paymentRequirements.get("network", "")
        if tx_hash:
            record_settlement(tx_hash, pay_to, amount, network)

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


_supported_cache: dict | None = None
_supported_etag: str | None = None


@app.get("/supported")
async def supported(request: Request):
    global _supported_cache, _supported_etag
    if _supported_cache is None:
        response = facilitator.get_supported()
        _supported_cache = {
            "kinds": [k.model_dump(by_alias=True, exclude_none=True) for k in response.kinds],
            "extensions": response.extensions,
            "signers": response.signers,
        }
        _supported_etag = hashlib.md5(json.dumps(_supported_cache, sort_keys=True).encode()).hexdigest()

    # ETag support — clients can cache and use conditional GET
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match.strip('"') == _supported_etag:
        return JSONResponse(status_code=304, content=None)

    return JSONResponse(
        content=_supported_cache,
        headers={"ETag": f'"{_supported_etag}"', "Cache-Control": "public, max-age=300"},
    )


@app.get("/settlements/{pay_to}")
async def get_settlements(
    pay_to: str,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Get settlement history for a merchant address."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT tx_hash, pay_to, amount, network, status, created_at FROM settlements WHERE pay_to = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (pay_to, limit, offset),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM settlements WHERE pay_to = ?", (pay_to,)).fetchone()[0]
    conn.close()
    return {
        "settlements": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/settlements/{pay_to}/stats")
async def get_settlement_stats(pay_to: str):
    """Get aggregate stats for a merchant address."""
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        "SELECT COUNT(*) as count, COALESCE(SUM(CAST(amount AS REAL)), 0) as total_amount FROM settlements WHERE pay_to = ? AND status = 'settled'",
        (pay_to,),
    ).fetchone()
    conn.close()
    return {"pay_to": pay_to, "total_settlements": row[0], "total_amount": row[1]}


@app.post("/verify-key")
async def verify_api_key(request: Request):
    """Check if an API key is valid. Returns the associated merchant if so.

    Sellers can use this to bypass the 402 flow for key-holding clients:
    check Authorization header -> if valid key, return 200 directly.
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    key = auth[7:]
    keys = _load_api_keys()
    if key not in keys:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return {"valid": True, "merchant": keys[key].get("merchant", ""), "note": keys[key].get("note", "")}


@app.get("/discovery/resources")
async def discovery():
    """Bazaar-compatible discovery endpoint. Lists x402-payable resources available through this facilitator."""
    return {
        "facilitator": "Satoshi",
        "url": "https://facilitator.bitcoinsapi.com",
        "version": "1.6.0",
        "resources": [
            {
                "url": "https://bitcoinsapi.com/api/v1/ai/explain-tx",
                "method": "POST",
                "description": "AI-powered Bitcoin transaction explainer",
                "price": "0.01",
                "currency": "USDC",
                "network": "eip155:8453",
            },
            {
                "url": "https://bitcoinsapi.com/api/v1/ai/explain-block",
                "method": "POST",
                "description": "AI-powered Bitcoin block analysis",
                "price": "0.01",
                "currency": "USDC",
                "network": "eip155:8453",
            },
            {
                "url": "https://bitcoinsapi.com/api/v1/ai/fee-advice",
                "method": "GET",
                "description": "AI fee optimization recommendation",
                "price": "0.01",
                "currency": "USDC",
                "network": "eip155:8453",
            },
            {
                "url": "https://bitcoinsapi.com/api/v1/ai/chat",
                "method": "POST",
                "description": "Bitcoin knowledge chatbot",
                "price": "0.01",
                "currency": "USDC",
                "network": "eip155:8453",
            },
            {
                "url": "https://bitcoinsapi.com/api/v1/broadcast",
                "method": "POST",
                "description": "Broadcast a signed Bitcoin transaction",
                "price": "0.01",
                "currency": "USDC",
                "network": "eip155:8453",
            },
            {
                "url": "https://bitcoinsapi.com/api/v1/mining/nextblock",
                "method": "GET",
                "description": "Next block prediction with fee analysis",
                "price": "0.01",
                "currency": "USDC",
                "network": "eip155:8453",
            },
            {
                "url": "https://bitcoinsapi.com/api/v1/fees/observatory/scoreboard",
                "method": "GET",
                "description": "Fee estimator accuracy scoreboard",
                "price": "0.005",
                "currency": "USDC",
                "network": "eip155:8453",
            },
            {
                "url": "https://bitcoinsapi.com/api/v1/fees/observatory/block-stats",
                "method": "GET",
                "description": "Per-block fee statistics",
                "price": "0.005",
                "currency": "USDC",
                "network": "eip155:8453",
            },
            {
                "url": "https://bitcoinsapi.com/api/v1/fees/observatory/estimates",
                "method": "GET",
                "description": "Fee estimate history from multiple sources",
                "price": "0.005",
                "currency": "USDC",
                "network": "eip155:8453",
            },
            {
                "url": "https://bitcoinsapi.com/api/v1/fees/landscape",
                "method": "GET",
                "description": "Complete fee landscape analysis",
                "price": "0.005",
                "currency": "USDC",
                "network": "eip155:8453",
            },
        ],
    }


@app.get("/llms.txt", response_class=JSONResponse)
async def llms_txt():
    """Machine-readable facilitator description for AI agents."""
    return JSONResponse(
        content={
            "name": "Satoshi x402 Facilitator",
            "description": "Multi-chain x402 payment facilitator supporting Base, Polygon, Arbitrum, Optimism, Solana + testnets. Verifies and settles USDC micropayments for API access.",
            "url": "https://facilitator.bitcoinsapi.com",
            "protocol": "x402",
            "supported_networks": ["eip155:8453", "eip155:84532", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"],
            "currency": "USDC",
            "extensions": ["eip2612GasSponsoring", "erc20ApprovalGasSponsoring", "bazaar"],
            "endpoints": {
                "verify": "POST /verify",
                "settle": "POST /settle",
                "supported": "GET /supported",
                "discovery": "GET /discovery/resources",
                "settlements": "GET /settlements/{payTo}",
                "stats": "GET /settlements/{payTo}/stats",
                "health": "GET /health",
            },
            "merchants": [
                {
                    "name": "Satoshi API",
                    "url": "https://bitcoinsapi.com",
                    "description": "Bitcoin fee intelligence API for AI agents",
                    "paid_endpoints": 10,
                }
            ],
        },
        media_type="application/json",
    )


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.6.0"}


if __name__ == "__main__":
    import uvicorn

    supported_networks = [k.network for k in facilitator.get_supported().kinds]
    print(f"Satoshi Facilitator listening on http://0.0.0.0:{PORT}")
    print(f"Networks: {', '.join(supported_networks)}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
