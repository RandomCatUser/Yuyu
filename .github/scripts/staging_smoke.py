"""Staging smoke test.

Boots the real dashboard app in-process and checks the things that only show up
when the whole thing is wired together, as opposed to the unit suite. Kept out of
tests/ on purpose: this is not a pytest file, and the project reserves
tests/probe_*.py for live-network diagnostics.

Run:  python .github/scripts/staging_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}{f' - {detail}' if detail else ''}")
        failures.append(label)


def main() -> int:
    from yuyu.dashboard.app import LOOPBACK, create_app, start_dashboard
    from yuyu.config import config

    app = create_app()
    client = app.test_client()

    print("read endpoints")
    for path in ("/", "/api/", "/api/models", "/api/setup"):
        resp = client.get(path)
        check(f"GET {path} -> 200", resp.status_code == 200, f"got {resp.status_code}")

    print("shell renders")
    body = client.get("/").get_data(as_text=True)
    check("index.html is not empty", bool(body.strip()))
    check("index.html is html", "<html" in body.lower() or "<!doctype" in body.lower())

    print("api returns json")
    resp = client.get("/api/")
    check("/api/ is json", resp.is_json, f"content-type {resp.content_type}")

    print("cross-origin write is refused")
    # There is no password, so the origin check on non-GET /api/ calls is the
    # only thing stopping a random page in a browser from rewriting her persona.
    resp = client.put("/api/named/persona", json={}, headers={"Origin": "https://evil.example"})
    check("cross-origin PUT /api/named/persona -> 403", resp.status_code == 403, f"got {resp.status_code}")

    print("path traversal is refused")
    for bad in ("../../.env", "..%2F..%2F.env", "..\\..\\.env"):
        resp = client.get(f"/api/file/{bad}")
        check(f"GET /api/file/{bad} is not 200", resp.status_code != 200, f"got {resp.status_code}")

    print("loopback bind is enforced")
    original = config["dashboard"]["host"]
    try:
        config["dashboard"]["host"] = "0.0.0.0"
        # start_dashboard() returns None before it ever calls app.run(), so this
        # is safe to call in-process.
        check("0.0.0.0 refuses to start", start_dashboard() is None)
    finally:
        config["dashboard"]["host"] = original

    check("configured host is loopback", original in LOOPBACK, f"host is {original!r}")

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("\nfailed:")
        for name in failures:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())