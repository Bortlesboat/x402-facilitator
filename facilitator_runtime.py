from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from x402 import x402Facilitator
from x402.extensions.bazaar import BAZAAR
from x402.extensions.eip2612_gas_sponsoring import EIP2612_GAS_SPONSORING
from x402.extensions.erc20_approval_gas_sponsoring import (
    Erc20ApprovalFacilitatorExtension,
    WriteContractCall,
)
from x402.mechanisms.evm import FacilitatorWeb3Signer
from x402.mechanisms.evm.exact.facilitator import ExactEvmScheme, ExactEvmSchemeConfig

from facilitator_metadata import FacilitatorMetadata, default_facilitator_metadata, network_label


@dataclass
class FacilitatorRuntimeState:
    facilitator: Any
    metadata: FacilitatorMetadata
    supported_kinds: list[dict[str, Any]]
    extensions: list[str]
    signers: dict[str, list[str]]
    pay_to_by_network: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_facilitator(
        cls,
        facilitator: x402Facilitator,
        metadata: FacilitatorMetadata,
        pay_to_by_network: dict[str, str],
    ) -> "FacilitatorRuntimeState":
        supported = facilitator.get_supported()
        return cls(
            facilitator=facilitator,
            metadata=metadata,
            supported_kinds=[
                kind.model_dump(by_alias=True, exclude_none=True) for kind in supported.kinds
            ],
            extensions=list(supported.extensions),
            signers=dict(supported.signers),
            pay_to_by_network=dict(pay_to_by_network),
        )

    def supported_networks(self) -> list[str]:
        return [kind["network"] for kind in self.supported_kinds]

    def supported_payload(self) -> dict[str, Any]:
        return {
            "kinds": self.supported_kinds,
            "extensions": self.extensions,
            "signers": self.signers,
        }

    async def verify_request(self, payment_payload: dict, payment_requirements: dict) -> dict[str, Any]:
        if hasattr(self.facilitator, "verify_request"):
            return await self.facilitator.verify_request(payment_payload, payment_requirements)

        from x402.schemas import PaymentRequirements, parse_payment_payload

        payload = parse_payment_payload(payment_payload)
        requirements = PaymentRequirements.model_validate(payment_requirements)
        response = await self.facilitator.verify(payload, requirements)
        return response.model_dump(by_alias=True, exclude_none=True)

    async def settle_request(self, payment_payload: dict, payment_requirements: dict) -> dict[str, Any]:
        if hasattr(self.facilitator, "settle_request"):
            return await self.facilitator.settle_request(payment_payload, payment_requirements)

        from x402.schemas import PaymentRequirements, parse_payment_payload

        payload = parse_payment_payload(payment_payload)
        requirements = PaymentRequirements.model_validate(payment_requirements)
        response = await self.facilitator.settle(payload, requirements)
        return response.model_dump(by_alias=True, exclude_none=True)

    def _iter_discovery_items(self, *, resource_type: str | None = None) -> list[dict[str, Any]]:
        live_networks = set(self.supported_networks())
        items: list[dict[str, Any]] = []
        for resource in self.metadata.resources:
            if resource.network not in live_networks:
                continue
            if resource_type and resource.resource_type != resource_type:
                continue
            pay_to = self.pay_to_by_network.get(resource.network)
            if not pay_to:
                continue
            items.append(self.metadata.build_discovery_item(resource, pay_to))
        return items

    @staticmethod
    def _matches_query(item: dict[str, Any], query: str | None) -> bool:
        if not query:
            return True
        terms = [term.strip().lower() for term in query.split() if term.strip()]
        if not terms:
            return True
        metadata = item.get("metadata") or {}
        searchable = " ".join(
            [
                item.get("resource", ""),
                item.get("description", ""),
                str(metadata.get("description", "")),
                str(metadata.get("priceUsd", "")),
                " ".join(str(value) for value in metadata.get("extensions", [])),
                " ".join(str(value) for value in metadata.get("keywords", [])),
                str(metadata.get("category", "")),
            ]
        ).lower()
        return all(term in searchable for term in terms)

    @staticmethod
    def _parse_max_usd_price(max_usd_price: str | None) -> int | None:
        if max_usd_price in (None, ""):
            return None
        try:
            return int(Decimal(str(max_usd_price)) * Decimal("1000000"))
        except (InvalidOperation, ValueError):
            return None

    @classmethod
    def _accept_matches(
        cls,
        accept: dict[str, Any],
        *,
        network: str | None,
        asset: str | None,
        scheme: str | None,
        pay_to: str | None,
        max_usd_price: str | None,
    ) -> bool:
        if network and accept.get("network") != network:
            return False
        if asset and accept.get("asset") != asset:
            return False
        if scheme and accept.get("scheme") != scheme:
            return False
        if pay_to and accept.get("payTo", "").lower() != pay_to.lower():
            return False
        max_amount = cls._parse_max_usd_price(max_usd_price)
        if max_amount is not None:
            try:
                amount = int(str(accept.get("amount", accept.get("maxAmountRequired", "0"))))
            except ValueError:
                return False
            if amount > max_amount:
                return False
        return True

    def discovery_payload(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        resource_type: str | None = None,
        query: str | None = None,
        network: str | None = None,
        asset: str | None = None,
        scheme: str | None = None,
        pay_to: str | None = None,
        max_usd_price: str | None = None,
        extensions: str | None = None,
    ) -> dict[str, Any]:
        items = self._iter_discovery_items(resource_type=resource_type)
        required_extensions = {
            part.strip().lower() for part in (extensions or "").split(",") if part.strip()
        }
        filtered: list[dict[str, Any]] = []
        for item in items:
            if not self._matches_query(item, query):
                continue
            metadata = item.get("metadata") or {}
            metadata_extensions = {
                str(value).lower() for value in metadata.get("extensions", [])
            }
            if required_extensions and not required_extensions.issubset(metadata_extensions):
                continue
            accepts = [
                accept
                for accept in item.get("accepts", [])
                if self._accept_matches(
                    accept,
                    network=network,
                    asset=asset,
                    scheme=scheme,
                    pay_to=pay_to,
                    max_usd_price=max_usd_price,
                )
            ]
            if not accepts:
                continue
            filtered_item = dict(item)
            filtered_item["accepts"] = accepts
            filtered.append(filtered_item)

        total = len(filtered)
        paginated_items = filtered[offset : offset + limit]
        return {
            "x402Version": self.metadata.discovery_version,
            "items": paginated_items,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
            },
        }

    def merchant_payload(self, *, pay_to: str, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        items = [
            item
            for item in self._iter_discovery_items()
            if any(accept.get("payTo", "").lower() == pay_to.lower() for accept in item.get("accepts", []))
        ]
        total = len(items)
        return {
            "payTo": pay_to,
            "resources": items[offset : offset + limit],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
            },
        }

    def llms_payload(self) -> dict[str, Any]:
        supported_networks = self.supported_networks()
        network_names = [network_label(network) for network in supported_networks]
        description_suffix = ", ".join(network_names) if network_names else "no configured networks"
        return {
            "name": self.metadata.name,
            "version": self.metadata.version,
            "description": (
                f"x402 payment facilitator for Satoshi API. Supports {description_suffix}. "
                "Verifies and settles USDC micropayments for premium API access."
            ),
            "url": self.metadata.public_url,
            "protocol": self.metadata.protocol,
            "supported_networks": supported_networks,
            "currency": "USDC",
            "extensions": self.extensions,
            "endpoints": self.metadata.endpoint_map,
            "merchants": self.metadata.merchant_summary(),
        }

    def health_payload(self) -> dict[str, Any]:
        return {"status": "ok", "version": self.metadata.version}

    def status_payload(self, *, observability: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": self.metadata.version,
            "url": self.metadata.public_url,
            "supported": self.supported_payload(),
            "resourceCount": len(self._iter_discovery_items()),
            "observability": observability,
        }


class Web3BatchSigner:
    def __init__(self, web3_signer: FacilitatorWeb3Signer) -> None:
        self._signer = web3_signer

    def send_transactions(self, transactions: list[str | WriteContractCall]) -> list[str]:
        hashes: list[str] = []
        for transaction in transactions:
            if isinstance(transaction, str):
                tx_hash = self._signer.w3.eth.send_raw_transaction(transaction).hex()
                self._signer.wait_for_transaction_receipt(tx_hash)
                hashes.append(tx_hash)
                continue
            tx_hash = self._signer.write_contract(
                transaction.address,
                transaction.abi,
                transaction.function,
                *transaction.args,
            )
            hashes.append(tx_hash)
        return hashes

    def wait_for_transaction_receipt(self, tx_hash: str) -> Any:
        return self._signer.wait_for_transaction_receipt(tx_hash)


def _register_evm_network(
    facilitator: x402Facilitator,
    signer: FacilitatorWeb3Signer,
    config: ExactEvmSchemeConfig,
    network: str,
    pay_to_by_network: dict[str, str],
) -> None:
    facilitator.register([network], ExactEvmScheme(signer, config))
    pay_to_by_network[network] = signer.get_addresses()[0]


def build_runtime_from_env() -> FacilitatorRuntimeState:
    metadata = default_facilitator_metadata()
    evm_private_key = os.environ.get("EVM_PRIVATE_KEY")
    svm_private_key = os.environ.get("SVM_PRIVATE_KEY")
    if not evm_private_key and not svm_private_key:
        raise RuntimeError("At least one of EVM_PRIVATE_KEY or SVM_PRIVATE_KEY is required")

    facilitator = x402Facilitator()
    pay_to_by_network: dict[str, str] = {}

    if evm_private_key:
        mainnet_signer = FacilitatorWeb3Signer(
            private_key=evm_private_key,
            rpc_url=os.environ.get("EVM_RPC_URL", "https://mainnet.base.org"),
        )
        config = ExactEvmSchemeConfig(deploy_erc4337_with_eip6492=True)
        _register_evm_network(facilitator, mainnet_signer, config, "eip155:8453", pay_to_by_network)
        facilitator.register_extension(EIP2612_GAS_SPONSORING)
        facilitator.register_extension(
            Erc20ApprovalFacilitatorExtension(signer=Web3BatchSigner(mainnet_signer))
        )
        facilitator.register_extension(BAZAAR)

        extra_chains = {
            "eip155:137": ("POLYGON_RPC_URL", "https://polygon.llamarpc.com", "ENABLE_POLYGON"),
            "eip155:42161": ("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc", "ENABLE_ARBITRUM"),
            "eip155:10": ("OPTIMISM_RPC_URL", "https://mainnet.optimism.io", "ENABLE_OPTIMISM"),
        }
        for network, (rpc_env, rpc_default, enable_env) in extra_chains.items():
            if not os.environ.get(enable_env):
                continue
            signer = FacilitatorWeb3Signer(
                private_key=evm_private_key,
                rpc_url=os.environ.get(rpc_env, rpc_default),
            )
            _register_evm_network(facilitator, signer, config, network, pay_to_by_network)

        testnet_signer = FacilitatorWeb3Signer(
            private_key=evm_private_key,
            rpc_url=os.environ.get("EVM_TESTNET_RPC_URL", "https://sepolia.base.org"),
        )
        _register_evm_network(facilitator, testnet_signer, config, "eip155:84532", pay_to_by_network)

    if svm_private_key:
        from solders.keypair import Keypair
        from x402.mechanisms.svm import FacilitatorKeypairSigner
        from x402.mechanisms.svm.exact.facilitator import ExactSvmScheme

        svm_signer = FacilitatorKeypairSigner(Keypair.from_base58_string(svm_private_key))
        facilitator.register(
            ["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"],
            ExactSvmScheme(svm_signer),
        )
        pay_to_by_network["solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"] = svm_signer.get_addresses()[0]
        facilitator.register(
            ["solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"],
            ExactSvmScheme(svm_signer),
        )
        pay_to_by_network["solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"] = svm_signer.get_addresses()[0]

    return FacilitatorRuntimeState.from_facilitator(
        facilitator=facilitator,
        metadata=metadata,
        pay_to_by_network=pay_to_by_network,
    )
