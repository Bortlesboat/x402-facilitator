"""Satoshi x402 Facilitator.

Multi-chain facilitator: Base, Polygon, Arbitrum, Optimism, Solana + testnets.
Gas sponsoring + bazaar extensions. x402 v1 + v2 support.

Run with: uvicorn main:app --host 0.0.0.0 --port 4022
"""

import hashlib
import json
import os
import sys

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
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

    # Additional EVM mainnets (same key, different RPCs)
    extra_chains = {
        "eip155:137": ("Polygon", os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")),
        "eip155:42161": ("Arbitrum", os.environ.get("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")),
        "eip155:10": ("Optimism", os.environ.get("OPTIMISM_RPC_URL", "https://mainnet.optimism.io")),
    }
    for chain_id, (name, rpc_url) in extra_chains.items():
        try:
            signer = FacilitatorWeb3Signer(private_key=evm_private_key, rpc_url=rpc_url)
            facilitator.register([chain_id], ExactEvmScheme(signer, config))
            print(f"{name} ({chain_id}) registered")
        except Exception as e:
            print(f"{name} ({chain_id}) failed: {e}")

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
    version="1.4.0",
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
        return response.model_dump(by_alias=True, exclude_none=True)
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


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.4.0"}


if __name__ == "__main__":
    import uvicorn

    supported_networks = [k.network for k in facilitator.get_supported().kinds]
    print(f"Satoshi Facilitator listening on http://0.0.0.0:{PORT}")
    print(f"Networks: {', '.join(supported_networks)}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
