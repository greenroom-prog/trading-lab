"""
venues.py — Kalshi + Coinbase credential loading, signing, and connectivity check.

Read-only by design. Order placement is deliberately absent; add it after
smoke_test() passes and after you have decided what you are trading.

Env (.env, chmod 600, gitignored):

    KALSHI_ENV=demo                      # demo | prod
    KALSHI_KEY_ID=<uuid from dashboard>
    KALSHI_PRIVATE_KEY_PATH=~/.secrets/kalshi_demo.pem
    KALSHI_BASE_DEMO=https://demo-api.kalshi.co/trade-api/v2
    KALSHI_BASE_PROD=https://api.elections.kalshi.com/trade-api/v2

    COINBASE_API_KEY=organizations/{org_id}/apiKeys/{key_id}
    COINBASE_PRIVATE_KEY_PATH=~/.secrets/coinbase_cdp.pem

Base URLs are env-configurable on purpose: Kalshi moved hosts and public
guides disagree about the current one. Verify against docs.kalshi.com and
put the answer in .env rather than hardcoding a guess here.

    pip install requests cryptography python-dotenv coinbase-advanced-py
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 15


def _require(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"{name} missing from environment")
    return v


def _read_key_file(env_name: str) -> str:
    p = Path(os.path.expanduser(_require(env_name)))
    if not p.exists():
        raise RuntimeError(f"{env_name} points at {p}, which does not exist")
    mode = p.stat().st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError(f"{p} is mode {oct(mode)}; run: chmod 600 {p}")
    return p.read_text()


# ---------------------------------------------------------------- Kalshi


class Kalshi:
    """Signed REST client. Signature covers timestamp_ms + METHOD + path,
    path including the /trade-api/v2 prefix and excluding the query string."""

    def __init__(self) -> None:
        env = os.getenv("KALSHI_ENV", "demo").lower()
        if env not in ("demo", "prod"):
            raise RuntimeError("KALSHI_ENV must be demo or prod")
        self.env = env
        self.base = _require(
            "KALSHI_BASE_PROD" if env == "prod" else "KALSHI_BASE_DEMO"
        ).rstrip("/")
        self.key_id = _require("KALSHI_KEY_ID")

        key = serialization.load_pem_private_key(
            _read_key_file("KALSHI_PRIVATE_KEY_PATH").encode(), password=None
        )
        if not isinstance(key, rsa.RSAPrivateKey):
            raise RuntimeError("Kalshi key is not an RSA private key")
        self._key = key

        # path prefix that must be included in the signed message
        self.prefix = "/" + self.base.split("://", 1)[1].split("/", 1)[1]

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = self._key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def get(self, endpoint: str, **params):
        endpoint = "/" + endpoint.lstrip("/")
        path = self.prefix + endpoint
        r = requests.get(
            self.base + endpoint,
            headers=self._headers("GET", path),
            params=params or None,
            timeout=TIMEOUT,
        )
        if r.status_code == 401:
            raise RuntimeError(
                "Kalshi 401. Signed path was: " + path + "\n"
                "Check: key registered in THIS environment (demo keys are not "
                "prod keys), system clock within tolerance, path prefix matches "
                "the base URL."
            )
        r.raise_for_status()
        return r.json()

    # read surface
    def balance(self):
        return self.get("/portfolio/balance")

    def markets(self, limit=20, status="open", **kw):
        return self.get("/markets", limit=limit, status=status, **kw)

    def orderbook(self, ticker, depth=10):
        return self.get(f"/markets/{ticker}/orderbook", depth=depth)

    def positions(self):
        return self.get("/portfolio/positions")


# -------------------------------------------------------------- Coinbase


def coinbase_client():
    """RESTClient from coinbase-advanced-py. Handles JWT signing itself;
    key type (Ed25519 / ECDSA) is auto-detected."""
    from coinbase.rest import RESTClient

    return RESTClient(
        api_key=_require("COINBASE_API_KEY"),
        api_secret=_read_key_file("COINBASE_PRIVATE_KEY_PATH"),
        timeout=TIMEOUT,
    )


# ------------------------------------------------------------ smoke test


def smoke_test() -> int:
    """Prove each credential works on a read endpoint before anything else
    is built on top of it. Returns 0 on all-pass, 1 otherwise."""
    failures = []

    print(f"[kalshi] env={os.getenv('KALSHI_ENV', 'demo')}")
    try:
        k = Kalshi()
        print(f"[kalshi] base={k.base}")
        mk = k.markets(limit=3)
        n = len(mk.get("markets", []))
        print(f"[kalshi] public read OK — {n} markets")
        bal = k.balance()
        print(f"[kalshi] auth read OK — balance={bal}")
    except Exception as e:
        failures.append(f"kalshi: {e}")
        print(f"[kalshi] FAIL {e}")

    try:
        c = coinbase_client()
        acc = c.get_accounts(limit=5)
        accounts = getattr(acc, "accounts", None) or acc.get("accounts", [])
        print(f"[coinbase] auth read OK — {len(accounts)} accounts")
        p = c.get_product("BTC-USD")
        price = getattr(p, "price", None) or p.get("price")
        print(f"[coinbase] market read OK — BTC-USD {price}")
    except Exception as e:
        failures.append(f"coinbase: {e}")
        print(f"[coinbase] FAIL {e}")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nall credentials live, read-only path confirmed")
    return 0


if __name__ == "__main__":
    raise SystemExit(smoke_test())
