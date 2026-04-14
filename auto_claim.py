"""
Playwright 辅助：在 Polymarket 网页上尝试 Redeem/Claim（见 TRADING_AND_SYSTEM_LOGIC.md §18）。
Requires: playwright install chromium
Session: log in manually once, then set POLYMARKET_STORAGE_STATE to a saved storage JSON path.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from dotenv import load_dotenv

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # type: ignore


def main() -> None:
    load_dotenv()
    if sync_playwright is None:
        print("请先安装: pip install playwright && playwright install chromium", file=sys.stderr)
        sys.exit(1)
    p = argparse.ArgumentParser()
    p.add_argument(
        "--url",
        default=os.environ.get("POLYMARKET_CLAIM_URL", "https://polymarket.com/portfolio"),
    )
    p.add_argument("--interval", type=int, default=120, help="seconds between scans")
    p.add_argument("--headed", action="store_true")
    args = p.parse_args()
    storage = os.environ.get("POLYMARKET_STORAGE_STATE")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = (
            browser.new_context(storage_state=storage)
            if storage and os.path.isfile(storage)
            else browser.new_context()
        )
        page = context.new_page()
        while True:
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
                for name in ("Redeem", "Claim", "Collect"):
                    loc = page.get_by_role("button", name=name)
                    if loc.count() > 0:
                        loc.first.click(timeout=5_000)
                        print(f"已点击按钮: {name}")
                        time.sleep(2)
            except Exception as e:
                print(f"领取扫描异常: {e}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
