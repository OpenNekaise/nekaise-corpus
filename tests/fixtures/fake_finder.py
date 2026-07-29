#!/usr/bin/env python3
"""Network-free finder fixture used to exercise run_round's subprocess proposal protocol."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import registry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args()
    entries = [{
        "id": args.id,
        "title": args.title,
        "url": args.url,
        "source": "fixture",
        "license": "cc-by",
        "topic": "construction",
        "format": "txt",
    }]
    print("# 1 fixture proposal")
    if args.append:
        registry.append_entries(entries)


if __name__ == "__main__":
    main()
