"""
一次性：从 POLY_PRIVATE_KEY 推导 Polymarket CLOB API 三字段（见 TRADING_AND_SYSTEM_LOGIC.md 附录 D）。
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from py_clob_client.client import ClobClient


def main() -> None:
    load_dotenv()
    key = os.environ.get("POLY_PRIVATE_KEY")
    if not key:
        print("请在 .env 中设置 POLY_PRIVATE_KEY", file=sys.stderr)
        sys.exit(1)
    host = os.environ.get("POLY_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = int(os.environ.get("POLY_CHAIN_ID", "137"))
    sig = int(os.environ.get("POLY_SIGNATURE_TYPE", "0"))
    funder = os.environ.get("POLY_FUNDER_ADDRESS") or None
    client = ClobClient(host, chain_id=chain_id, key=key, signature_type=sig, funder=funder)
    creds = client.create_or_derive_api_creds()
    print("请将以下内容追加到 .env：\n")
    print(f"POLY_API_KEY={creds.api_key}")
    print(f"POLY_API_SECRET={creds.api_secret}")
    print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")


if __name__ == "__main__":
    main()
