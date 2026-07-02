"""
Hit every backend endpoint and print a simple pass/fail report.

Run while the backend is up:
    python tests/test_connection.py
"""

from __future__ import annotations

import http.client
import json
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


BASE_URL = "http://localhost:8000"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class Endpoint:
    method: str
    path: str
    name: str
    expect_type: type[Any] | tuple[type[Any], ...] = dict
    body: dict[str, Any] | None = None


ENDPOINTS = [
    Endpoint("GET", "/api/ping", "Ping", dict),
    Endpoint("GET", "/api/account/summary", "Account summary", dict),
    Endpoint("GET", "/api/signals", "Signal scores", list),
    Endpoint("GET", "/api/top-picks", "Top picks", list),
    Endpoint("GET", "/api/trades/open", "Open trades", list),
    Endpoint("GET", "/api/trades/history", "Trade history", list),
    Endpoint("GET", "/api/strategy", "Strategy", (dict, type(None))),
    Endpoint("OPTIONS", "/api/ping", "CORS preflight", dict),
]


def request_json(endpoint: Endpoint) -> tuple[int, Any]:
    parsed = urlparse(BASE_URL)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)
    try:
        body = json.dumps(endpoint.body).encode("utf-8") if endpoint.body else None
        headers = {
            "Origin": "http://localhost:5173",
            "Content-Type": "application/json",
        }
        conn.request(endpoint.method, endpoint.path, body=body, headers=headers)
        response = conn.getresponse()
        response_body = response.read().decode("utf-8")
        try:
            payload = json.loads(response_body) if response_body else None
        except json.JSONDecodeError:
            payload = response_body
        return response.status, payload
    finally:
        conn.close()


def print_result(endpoint: Endpoint, status: int, payload: Any, ok: bool) -> None:
    icon = "✅" if ok else "❌"
    print(
        f"{icon} {endpoint.name:<22} {endpoint.method:<7} "
        f"{endpoint.path:<34} status={status}"
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    print("")


def check_endpoint(endpoint: Endpoint) -> tuple[bool, Any]:
    status, payload = request_json(endpoint)
    ok = 200 <= status < 300 and isinstance(payload, endpoint.expect_type)
    print_result(endpoint, status, payload, ok)
    return ok, payload


def main() -> int:
    failures = 0
    print(f"Checking backend at {BASE_URL}\n")

    for endpoint in ENDPOINTS:
        try:
            ok, _ = check_endpoint(endpoint)
            if not ok:
                failures += 1
        except Exception as exc:
            failures += 1
            print(f"❌ {endpoint.name:<22} {endpoint.method:<7} {endpoint.path:<34} {exc}")

    pipeline_steps = [
        Endpoint("POST", "/api/pipeline/run-indicators", "Run indicators", dict),
        Endpoint("POST", "/api/pipeline/run-scan", "Run scan", dict),
    ]

    scan_payload: Any = None
    for endpoint in pipeline_steps:
        try:
            ok, payload = check_endpoint(endpoint)
            if endpoint.path.endswith("/run-scan"):
                scan_payload = payload
            if not ok:
                failures += 1
        except Exception as exc:
            failures += 1
            print(f"❌ {endpoint.name:<22} {endpoint.method:<7} {endpoint.path:<34} {exc}")

    top_picks = scan_payload.get("top_picks", []) if isinstance(scan_payload, dict) else []
    execute_symbol = top_picks[0]["symbol"] if top_picks else "RELIANCE"
    if not top_picks:
        print("No top pick returned by scan; executing RELIANCE to verify the endpoint response path.\n")

    remaining_pipeline_steps = [
        Endpoint(
            "POST",
            "/api/pipeline/execute-trade",
            f"Execute {execute_symbol}",
            dict,
            {"symbol": execute_symbol},
        ),
        Endpoint("POST", "/api/pipeline/monitor-trades", "Monitor trades", dict),
        Endpoint("GET", "/api/pipeline/status", "Pipeline status", dict),
        Endpoint("POST", "/api/pipeline/run-full-cycle", "Run full cycle", dict),
    ]

    for endpoint in remaining_pipeline_steps:
        try:
            ok, _ = check_endpoint(endpoint)
            if not ok:
                failures += 1
        except Exception as exc:
            failures += 1
            print(f"❌ {endpoint.name:<22} {endpoint.method:<7} {endpoint.path:<34} {exc}")

    if failures:
        print(f"❌ {failures} endpoint check(s) failed.")
        return 1

    print("✅ All backend endpoint checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
