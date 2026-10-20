"""Run the gateway with uvicorn.

`process_before_response` semantics do not apply here: uvicorn is a long-lived
ASGI server, so the handler simply completes its work before returning.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "mushroom_gateway.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - container-local
        port=int(os.environ.get("PORT", "8080")),
        # Replicas are scaled by Kubernetes, not by in-process workers, so one
        # worker per pod keeps the pool sizing and metrics honest.
        workers=1,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
