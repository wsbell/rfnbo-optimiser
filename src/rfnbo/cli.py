"""Command-line entry point: ``rfnbo validate <config.json>``."""

from __future__ import annotations

import argparse
import sys

from rfnbo.config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rfnbo")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="validate a plant config JSON file")
    v.add_argument("path")
    args = parser.parse_args(argv)

    if args.command == "validate":
        try:
            cfg = load_config(args.path)
        except ConfigError as exc:
            print(f"{args.path}: {len(exc.issues)} issue(s)", file=sys.stderr)
            for issue in exc.issues:
                print(f"  {issue}", file=sys.stderr)
            return 1
        print(
            f"{args.path}: valid ({cfg.plant} v{cfg.version}, effective {cfg.effective_from}, "
            f"{len(cfg.blocks)} blocks, {len(cfg.flows)} flows)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
