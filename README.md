# Satoshi x402 Facilitator

Satoshi x402 Facilitator is the payment facilitator used by Satoshi API for x402 machine-payment flows. It exposes verification, settlement, discovery, health, status, settlement-history, and observability endpoints for paid Bitcoin API resources.

Public service: https://facilitator.bitcoinsapi.com

## What It Provides

- `POST /verify` for x402 payment verification.
- `POST /settle` for payment settlement.
- `GET /supported` for supported network and asset metadata.
- `GET /discovery/resources` and `GET /discovery/merchant` for Bazaar-compatible discovery.
- `GET /health`, `GET /status`, and `GET /observability/summary` for operational checks.
- `GET /settlements/{payTo}` and `GET /settlements/{payTo}/stats` for settlement visibility.

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python main.py
```

The app listens on port `4022` by default. Set `PORT` to override it.

## Configuration

Common environment variables:

- `FACILITATOR_PUBLIC_URL`: public facilitator origin.
- `SELLER_ORIGIN`: seller/API origin used in discovery resources.
- `FACILITATOR_VERSION`: service metadata version.
- `SETTLEMENT_DB`: SQLite settlement database path.
- `API_KEYS_FILE`: optional JSON file for `/verify-key`.

Keep local keys, wallets, settlement databases, and deployment secrets outside the repository.

## Tests

```bash
python -m pytest
```
