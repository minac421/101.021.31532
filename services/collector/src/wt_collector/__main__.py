"""Command line: ``wt-collector {run,check-config,plan,read}``.

Read-only by construction: the collector only calls public market-data
endpoints, holds no API keys and has no code path that can place orders.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import ssl
import sys
from collections.abc import Sequence
from pathlib import Path

import msgspec

from wt_collector.app import Collector, rank_by_quote_volume, select_focus, select_universe
from wt_collector.config import CollectorConfig, load_config
from wt_data.binance.um.messages import exchange_info_decoder, tickers_decoder
from wt_data.binance.um.streams import build_groups, plan_connections
from wt_data.recorder import read_records


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wt-collector",
        description="Read-only Binance USD-M market data recorder (public data only: no keys, no orders).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="record market data until SIGINT/SIGTERM")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--log-level", default="INFO")
    check = sub.add_parser("check-config", help="validate a configuration file")
    check.add_argument("--config", type=Path, required=True)
    plan = sub.add_parser(
        "plan", help="show the connection plan computed from saved REST responses (offline)"
    )
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument(
        "--exchange-info", type=Path, required=True, help="saved GET /fapi/v1/exchangeInfo JSON"
    )
    plan.add_argument("--tickers", type=Path, help="saved GET /fapi/v1/ticker/24hr JSON (for focus ranking)")
    read = sub.add_parser("read", help="print records from a recorder file (.jsonl.zst)")
    read.add_argument("path", type=Path)
    read.add_argument("--limit", type=int, default=20)
    return parser


def _print_plan(cfg: CollectorConfig, exchange_info: Path, tickers: Path | None) -> None:
    info = exchange_info_decoder.decode(exchange_info.read_bytes())
    universe = select_universe(info, cfg.universe)
    ranked = rank_by_quote_volume(universe, tickers_decoder.decode(tickers.read_bytes()) if tickers else [])
    if cfg.universe.max_symbols is not None:
        keep = set(ranked[: cfg.universe.max_symbols]) | set(cfg.universe.include)
        universe = sorted(s for s in universe if s in keep)
    focus = select_focus(universe, ranked, cfg.focus)
    plan = plan_connections(
        build_groups(
            symbols=universe,
            focus_symbols=focus,
            per_symbol=cfg.streams.per_symbol,
            focus_streams=cfg.streams.focus,
            market_wide=cfg.streams.market_wide,
            routes=cfg.exchange.effective_routes,
        ),
        max_streams_per_connection=cfg.exchange.max_streams_per_connection,
        max_url_length=cfg.exchange.max_url_length,
        ws_base_url=cfg.exchange.ws_base_url,
    )
    print(f"universe: {len(universe)} symbols; focus ({len(focus)}): {', '.join(focus)}")
    print(f"{'connection':<36} {'route':<8} {'streams':>7} {'url chars':>9}")
    for spec in plan:
        url_chars = len(spec.url(cfg.exchange.ws_base_url))
        print(f"{spec.name:<36} {spec.route.value:<8} {len(spec.streams):>7} {url_chars:>9}")
    print(f"total: {len(plan)} connections, {sum(len(s.streams) for s in plan)} streams")


async def _run(cfg: CollectorConfig) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    ssl_context = ssl.create_default_context(cafile=cfg.rest.ca_bundle) if cfg.rest.ca_bundle else None
    await Collector(cfg, ssl_context=ssl_context).run(stop)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "read":
        for i, record in enumerate(read_records(args.path)):
            if i >= args.limit:
                break
            sys.stdout.write(msgspec.json.encode(record).decode() + "\n")
        return 0
    try:
        cfg = load_config(args.config)
    except (OSError, msgspec.ValidationError, msgspec.DecodeError, ValueError) as exc:
        print(f"invalid configuration {args.config}: {exc}", file=sys.stderr)
        return 2
    if args.command == "check-config":
        print(f"configuration OK: {args.config}")
        return 0
    if args.command == "plan":
        _print_plan(cfg, args.exchange_info, args.tickers)
        return 0
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_run(cfg))


if __name__ == "__main__":
    raise SystemExit(main())
