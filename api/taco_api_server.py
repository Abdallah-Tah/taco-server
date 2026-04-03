#!/usr/bin/env python3
"""
Compatibility API shim for older TacoClaw service units.

This process no longer computes dashboard state itself. Instead, it proxies
requests to the live backend used by the production dashboard:
`report_webhook.py` on 127.0.0.1:18791.
"""

from __future__ import annotations

import os
from typing import Iterator
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)

LIVE_API = os.environ.get("LIVE_API", "http://127.0.0.1:18791")
AUTH_TOKEN = os.environ.get("TACO_API_TOKEN", "your-secret-token-here")


def _target(path: str) -> str:
    return urljoin(f"{LIVE_API.rstrip('/')}/", path.lstrip("/"))


def _headers() -> dict[str, str]:
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    return headers


def _json_proxy(path: str) -> Response:
    try:
        upstream = requests.get(_target(path), headers=_headers(), timeout=30)
        return Response(
            upstream.content,
            status=upstream.status_code,
            content_type=upstream.headers.get("Content-Type", "application/json"),
        )
    except requests.RequestException as exc:
        return jsonify({"error": str(exc), "upstream": LIVE_API}), 502


@app.get("/")
def index() -> Response:
    return jsonify(
        {
            "status": "ok",
            "service": "taco-api-compat",
            "upstream": LIVE_API,
        }
    )


@app.get("/health")
def health() -> Response:
    return _json_proxy("/health")


@app.get("/system")
def system() -> Response:
    return _json_proxy("/system")


@app.get("/latest-sale")
def latest_sale() -> Response:
    return _json_proxy("/latest-sale")


@app.get("/redeemed")
def redeemed() -> Response:
    return _json_proxy("/redeemed")


@app.route("/api/<path:path>", methods=["GET"])
def api_proxy(path: str) -> Response:
    return _json_proxy(f"/api/{path}")


@app.get("/api/events/stream")
def events_stream() -> Response:
    try:
        upstream = requests.get(_target("/api/events/stream"), headers=_headers(), stream=True, timeout=30)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc), "upstream": LIVE_API}), 502

    def generate() -> Iterator[bytes]:
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "text/event-stream"),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
