"""Satoshi x402 Facilitator entrypoint."""

from __future__ import annotations

import os
import sys

from app_factory import create_app


PORT = int(os.environ.get("PORT", "4022"))

try:
    app = create_app()
except RuntimeError as exc:
    print(str(exc))
    sys.exit(1)


if __name__ == "__main__":
    import uvicorn

    runtime = app.state.runtime
    supported_networks = runtime.supported_networks()
    print(f"Satoshi Facilitator listening on http://0.0.0.0:{PORT}")
    print(f"Networks: {', '.join(supported_networks)}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
