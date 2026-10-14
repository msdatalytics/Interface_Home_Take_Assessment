"""Run the mock CoreBank tenants, or arm faults against a running instance.

python -m mock_corebank serve                  # cu-alpha on :8401, cu-beta on :8402
python -m mock_corebank fault cu-alpha maintenance_notice --path mbr_detail
python -m mock_corebank reset cu-alpha
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

import uvicorn

from mock_corebank.app import create_app
from mock_corebank.data import TENANTS
from mock_corebank.faults import FaultKind

DEFAULT_PORTS = {"cu-alpha": 8401, "cu-beta": 8402}
DEFAULT_USERNAME = "teller01"
DEFAULT_PASSWORD = "Teller#2024!"  # synthetic credential for the local mock only


async def _serve(host: str) -> None:
    username = os.environ.get("COREBANK_USERNAME", DEFAULT_USERNAME)
    password = os.environ.get("COREBANK_PASSWORD", DEFAULT_PASSWORD)
    servers = []
    for tenant_id, port in DEFAULT_PORTS.items():
        app = create_app(TENANTS[tenant_id], username=username, password=password)
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        servers.append(uvicorn.Server(config))
        print(f"[mock-corebank] {tenant_id:9s} -> http://{host}:{port}/", flush=True)
    await asyncio.gather(*(s.serve() for s in servers))


def _admin(tenant: str, method: str, path: str, body: dict[str, object] | None = None) -> None:
    url = f"http://127.0.0.1:{DEFAULT_PORTS[tenant]}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - localhost harness only
            print(resp.read().decode())
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"mock core for {tenant} is not reachable at {url} ({exc.reason}); start it with `mock-corebank serve`"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mock_corebank")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="serve both tenants")
    serve.add_argument("--host", default="127.0.0.1")

    fault = sub.add_parser("fault", help="arm a fault on a running tenant")
    fault.add_argument("tenant", choices=sorted(DEFAULT_PORTS))
    fault.add_argument("kind", choices=[k.value for k in FaultKind])
    fault.add_argument("--count", type=int, default=1)
    fault.add_argument("--path", dest="path_contains")
    fault.add_argument("--seconds", type=float, default=3.0)

    for name in ("reset", "audit", "clear"):
        p = sub.add_parser(name)
        p.add_argument("tenant", choices=sorted(DEFAULT_PORTS))

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        asyncio.run(_serve(args.host))
    elif args.cmd == "fault":
        body = {"kind": args.kind, "count": args.count, "seconds": args.seconds}
        if args.path_contains:
            body["path_contains"] = args.path_contains
        _admin(args.tenant, "POST", "/__admin/faults", body)
    elif args.cmd == "reset":
        _admin(args.tenant, "POST", "/__admin/reset")
    elif args.cmd == "clear":
        _admin(args.tenant, "DELETE", "/__admin/faults")
    elif args.cmd == "audit":
        _admin(args.tenant, "GET", "/__admin/audit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
