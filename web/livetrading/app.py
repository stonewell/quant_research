#!/usr/bin/env python3
"""CLI Entrypoint for Live Trading Deployment Web Application.

Usage:
    # 1. Run with defaults (port 8080, auto-discovered repo root):
    uv run --project web/livetrading python app.py

    # 2. Run on custom port and host:
    uv run --project web/livetrading python app.py --port 8888 --host 0.0.0.0

    # 3. Run with custom archive and strategy paths:
    uv run --project web/livetrading python app.py \\
        --strategy-dir /path/to/strategies \\
        --archive-dir /path/to/daily_archives
"""

import argparse
import os
import sys

# Ensure web/livetrading is on sys.path
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

import uvicorn
from server import create_app
from storage import StorageManager


def parse_args():
    parser = argparse.ArgumentParser(
        description="Production-grade Live Trading Deployment Web Application"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    parser.add_argument("--reload", action="store_true", help="Enable live auto-reload for development")

    # Configurable paths (zero hardcoded paths)
    parser.add_argument("--repo-root", default=None, help="Root directory of quant workspace (default: auto-detected)")
    parser.add_argument("--data-dir", default=None, help="Base data directory (default: web/livetrading/data)")
    parser.add_argument("--archive-dir", default=None, help="Daily run archives folder (default: data_dir/runs)")
    parser.add_argument("--strategy-dir", default=None, help="Strategy JSON dumps directory")
    parser.add_argument("--universe-dir", default=None, help="Pre-defined universe files directory")
    parser.add_argument("--cache-dir", default=None, help="Market data DuckDB / parquet cache directory")
    parser.add_argument("--stage1-script", default=None, help="Path to Stage 1 Health Check runner script")
    parser.add_argument("--stage2-script", default=None, help="Path to Stage 2 Live Deployment runner script")
    parser.add_argument("--python-bin", default=None, help="Python executable for running subprocesses")

    return parser.parse_args()


def main():
    args = parse_args()

    storage = StorageManager(
        repo_root=args.repo_root,
        data_dir=args.data_dir,
        archive_dir=args.archive_dir,
        strategy_dir=args.strategy_dir,
        universe_dir=args.universe_dir,
        cache_dir=args.cache_dir,
        stage1_script=args.stage1_script,
        stage2_script=args.stage2_script,
        python_executable=args.python_bin,
    )

    app = create_app(storage)

    print("=" * 80)
    print("LIVE TRADING DEPLOYMENT DASHBOARD")
    print("实盘量化交易调仓与风控部署平台")
    print("=" * 80)
    print(f"Server URL       : http://{args.host}:{args.port}")
    print(f"API Docs         : http://{args.host}:{args.port}/docs")
    print(f"Repo Root        : {storage.repo_root}")
    print(f"Data Dir         : {storage.data_dir}")
    print(f"Archive Dir      : {storage.archive_dir}")
    print(f"Strategy Dir     : {storage.strategy_dir}")
    print(f"Universe Dir     : {storage.universe_dir}")
    print(f"Python Executable: {storage.python_executable}")
    print("=" * 80)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
