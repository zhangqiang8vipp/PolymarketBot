#!/usr/bin/env python3
"""
Polymarket Bot - Entry Point

Usage:
    python -m core.bot --dry-run
    python -m core.bot --dry-run --once
    python -m core.bot --reset-history
"""

from core.bot import main

if __name__ == "__main__":
    main()
