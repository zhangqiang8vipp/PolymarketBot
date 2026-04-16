#!/usr/bin/env python3
"""
Setup Credentials - Configure API keys and secrets
Usage: python scripts/setup_creds.py
"""

import os
import json
from pathlib import Path

def setup():
    """Interactive credential setup."""
    print("=" * 50)
    print("Polymarket Bot - Credential Setup")
    print("=" * 50)

    # Check for existing .env
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        print(f"\n.env file exists at: {env_file}")
        with open(env_file) as f:
            print(f.read())
        print("\nTo update, edit the .env file directly or delete it and run this script again.")

    # Required credentials
    creds = {
        "POLYMARKET_API_KEY": input("Enter Polymarket API Key: ").strip(),
        "POLYMARKET_API_SECRET": input("Enter Polymarket API Secret: ").strip(),
    }

    # Optional credentials
    print("\nOptional settings (press Enter to skip):")
    poly_pid = input("Polymarket PID (for auto-claim): ").strip()
    if poly_pid:
        creds["POLYMARKET_PID"] = poly_pid

    # Save to .env
    with open(env_file, 'w') as f:
        for k, v in creds.items():
            if v:
                f.write(f"{k}={v}\n")

    print(f"\nCredentials saved to: {env_file}")
    print("IMPORTANT: Never commit .env to version control!")

if __name__ == "__main__":
    setup()
