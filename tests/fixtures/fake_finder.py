#!/usr/bin/env python3
"""Network-free finder fixture used to exercise run_round's subprocess proposal protocol."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import registry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--token")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--hold-rotation", action="store_true")
    parser.add_argument("--next-pointer")
    parser.add_argument("--exhausted")
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args()
    if args.exit_code:
        print(f"fixture failure {args.exit_code}", file=sys.stderr)
        raise SystemExit(args.exit_code)
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
    if args.hold_rotation:
        Path(os.environ["NEKAISE_ROTATION_HOLD_FILE"]).write_text("fixture hold\n")
    if args.next_pointer:
        Path(os.environ["NEKAISE_ROTATION_NEXT_FILE"]).write_text(args.next_pointer + "\n")
    if args.exhausted:
        Path(os.environ["NEKAISE_BACKEND_EXHAUSTED_FILE"]).write_text(args.exhausted + "\n")
    if args.append:
        registry.append_entries(entries)


if __name__ == "__main__":
    main()
