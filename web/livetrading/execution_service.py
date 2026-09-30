"""Execution service for invoking Stage 1 Health Check and Stage 2 Live Deployment.

Runs scripts via isolated Python subprocesses with live output capture and automatic archival.
"""

import json
import os
import subprocess
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:
    from .storage import StorageManager
except (ImportError, ValueError):
    from storage import StorageManager


class ExecutionService:
    """Orchestrates health check and live deployment runs."""

    def __init__(self, storage: StorageManager):
        self.storage = storage

    def _resolve_universe(
        self,
        universe_file: Optional[str],
        custom_symbols: Optional[List[str]],
        run_dir: str,
    ) -> str:
        """Resolve universe file path or materialize custom symbols to a universe file."""
        if custom_symbols and len(custom_symbols) > 0:
            custom_path = os.path.join(run_dir, "resolved_universe.txt")
            with open(custom_path, "w", encoding="utf-8") as f:
                f.write(f"# Custom universe generated {datetime.now().isoformat()}\n")
                for s in custom_symbols:
                    if s and s.strip():
                        f.write(f"{s.strip().upper()}\n")
            return custom_path

        if universe_file:
            if os.path.isabs(universe_file) and os.path.exists(universe_file):
                return universe_file
            candidate = os.path.join(self.storage.repo_root, universe_file)
            if os.path.exists(candidate):
                return candidate
            candidate_u = os.path.join(self.storage.universe_dir, universe_file)
            if os.path.exists(candidate_u):
                return candidate_u
            return universe_file

        # Default fallback: China Core-Satellite 22
        default_u = os.path.join(self.storage.repo_root, "docs", "universe", "china", "core_satellite_22_stocks.txt")
        return default_u

    def _resolve_strategy(self, strategy_file: str) -> str:
        """Resolve strategy file path relative to strategy_dir or repo_root."""
        if os.path.isabs(strategy_file) and os.path.exists(strategy_file):
            return strategy_file
        candidate = os.path.join(self.storage.strategy_dir, strategy_file)
        if os.path.exists(candidate):
            return candidate
        candidate_repo = os.path.join(self.storage.repo_root, strategy_file)
        if os.path.exists(candidate_repo):
            return candidate_repo
        return strategy_file

    def _get_subprocess_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        # Ensure repo root is on PYTHONPATH for common/ and pipeline/ imports
        existing_pythonpath = env.get("PYTHONPATH", "")
        paths = [self.storage.repo_root]
        if existing_pythonpath:
            paths.append(existing_pythonpath)
        env["PYTHONPATH"] = os.path.pathsep.join(paths)
        return env

    def run_health_check(
        self,
        strategy_file: str,
        universe_file: Optional[str] = None,
        custom_symbols: Optional[List[str]] = None,
        as_of_date: Optional[str] = None,
        portfolio_value: float = 100000.0,
        peak_nav: Optional[float] = None,
        data_provider: str = "synthetic",
    ) -> Dict[str, Any]:
        """Execute Stage 1 Health Check."""
        val_date = as_of_date or date.today().isoformat()
        run_dir = os.path.join(self.storage.archive_dir, val_date)
        os.makedirs(run_dir, exist_ok=True)

        strat_path = self._resolve_strategy(strategy_file)
        univ_path = self._resolve_universe(universe_file, custom_symbols, run_dir)
        account_state_file = os.path.join(run_dir, "account_state.json")

        # Copy global account state to daily run_dir if not present
        if not os.path.exists(account_state_file) and os.path.exists(self.storage.account_state_file):
            try:
                state_data = self.storage.get_account_state()
                self.storage._save_json(account_state_file, state_data)
            except Exception:
                pass

        cmd = [
            self.storage.python_executable,
            self.storage.stage1_script,
            "--strategy-file", strat_path,
            "--universe-file", univ_path,
            "--as-of-date", val_date,
            "--portfolio-value", str(portfolio_value),
            "--output-dir", run_dir,
            "--account-state-file", account_state_file,
            "--data-provider", data_provider,
        ]

        if peak_nav is not None and peak_nav > 0:
            cmd.extend(["--peak-nav", str(peak_nav)])
        if self.storage.cache_dir:
            cmd.extend(["--cache-dir", self.storage.cache_dir])

        print(f"[ExecutionService] Running Stage 1 Health Check: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            cwd=self.storage.repo_root,
            env=self._get_subprocess_env(),
            capture_output=True,
            text=True,
        )

        stdout = proc.stdout
        stderr = proc.stderr
        success = proc.returncode == 0

        # Load reports
        health_rep_file = os.path.join(run_dir, "stage1_health_report.json")
        health_data = self.storage._load_json(health_rep_file, default=None)
        if health_data and isinstance(health_data, dict):
            # Normalize fields for UI and API compatibility
            if "directive" not in health_data:
                health_data["directive"] = health_data.get("gate_code", "UNKNOWN")
            if "directive_label" not in health_data:
                health_data["directive_label"] = health_data.get("gate_headline", "")
            if "portfolio_health" in health_data:
                ph = health_data["portfolio_health"]
                health_data.setdefault("current_nav", ph.get("current_nav"))
                health_data.setdefault("peak_nav", ph.get("peak_nav"))
                health_data.setdefault("drawdown", ph.get("drawdown_pct"))
                health_data.setdefault("circuit_breaker_tier", ph.get("tier"))
                health_data.setdefault("linear_equity_scale", ph.get("linear_equity_scale"))
            if "market_macro" in health_data:
                mm = health_data["market_macro"]
                health_data.setdefault("market_breadth_50d", mm.get("breadth"))
                health_data.setdefault("breadth_thrust_10d", mm.get("thrust"))
                health_data.setdefault("target_exposure", mm.get("target_exposure"))
            if "volatility_targeting" in health_data:
                vt = health_data["volatility_targeting"]
                health_data.setdefault("realized_vol_21d", vt.get("realized_vol_21d"))
                health_data.setdefault("vol_scaling_factor", vt.get("vol_scaling_factor"))
            if "trader_actions" in health_data and "directive_summary" not in health_data:
                health_data["directive_summary"] = " \n".join(health_data["trader_actions"][:2])
            # Re-save normalized version
            self.storage._save_json(health_rep_file, health_data)

        state_data = self.storage._load_json(account_state_file, default=None)

        # Update global account state
        if state_data:
            self.storage.save_account_state(state_data)

        # Write summary.json
        summary_file = os.path.join(run_dir, "summary.json")
        summary = self.storage._load_json(summary_file, default={})
        summary.update({
            "timestamp": datetime.now().isoformat(),
            "date": val_date,
            "strategy_file": strat_path,
            "universe_file": univ_path,
            "portfolio_value": portfolio_value,
            "data_provider": data_provider,
            "health_status": "SUCCESS" if success else "FAILED",
            "health_returncode": proc.returncode,
            "health_stdout": stdout,
            "health_stderr": stderr,
        })
        self.storage._save_json(summary_file, summary)

        return {
            "status": "success" if success else "error",
            "message": "Stage 1 Health Check executed successfully" if success else f"Execution failed with code {proc.returncode}",
            "date": val_date,
            "stdout": stdout,
            "stderr": stderr,
            "health_report": health_data,
            "account_state": state_data,
        }

    def run_live_deploy(
        self,
        strategy_file: str,
        universe_file: Optional[str] = None,
        custom_symbols: Optional[List[str]] = None,
        as_of_date: Optional[str] = None,
        portfolio_value: float = 100000.0,
        current_holdings: Optional[Dict[str, Any]] = None,
        data_provider: str = "synthetic",
        lot_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute Stage 2 Live Deployment."""
        val_date = as_of_date or date.today().isoformat()
        run_dir = os.path.join(self.storage.archive_dir, val_date)
        os.makedirs(run_dir, exist_ok=True)

        strat_path = self._resolve_strategy(strategy_file)
        univ_path = self._resolve_universe(universe_file, custom_symbols, run_dir)
        account_state_file = os.path.join(run_dir, "account_state.json")
        health_rep_file = os.path.join(run_dir, "stage1_health_report.json")

        # Resolve holdings
        if current_holdings is not None:
            effective_holdings = current_holdings
        else:
            effective_holdings = self.storage.get_holdings()

        # Save holdings snapshot in run_dir and globally
        holdings_file = os.path.join(run_dir, "holdings.json")
        self.storage.save_holdings(effective_holdings, date_str=val_date)

        cmd = [
            self.storage.python_executable,
            self.storage.stage2_script,
            "--strategy-file", strat_path,
            "--universe-file", univ_path,
            "--as-of-date", val_date,
            "--portfolio-value", str(portfolio_value),
            "--current-holdings-file", holdings_file,
            "--output-dir", run_dir,
            "--account-state-file", account_state_file,
            "--data-provider", data_provider,
        ]

        if os.path.exists(health_rep_file):
            cmd.extend(["--health-report-file", health_rep_file])
        if lot_size is not None and lot_size > 0:
            cmd.extend(["--lot-size", str(lot_size)])
        if self.storage.cache_dir:
            cmd.extend(["--cache-dir", self.storage.cache_dir])

        print(f"[ExecutionService] Running Stage 2 Live Deployment: {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            cwd=self.storage.repo_root,
            env=self._get_subprocess_env(),
            capture_output=True,
            text=True,
        )

        stdout = proc.stdout
        stderr = proc.stderr
        success = proc.returncode == 0

        # Load generated trading ticket CSV and convert to JSON
        csv_file = os.path.join(run_dir, "live_trading_ticket.csv")
        ticket_json_file = os.path.join(run_dir, "live_trading_ticket.json")
        ticket_records = []
        if os.path.exists(csv_file):
            try:
                df = pd.read_csv(csv_file)
                ticket_records = df.to_dict(orient="records")
                self.storage._save_json(ticket_json_file, ticket_records)
            except Exception as e:
                print(f"[ExecutionService] Error converting ticket CSV to JSON: {e}")

        state_data = self.storage._load_json(account_state_file, default=None)
        health_data = self.storage._load_json(health_rep_file, default=None)

        if state_data:
            self.storage.save_account_state(state_data)

        # Update summary.json
        summary_file = os.path.join(run_dir, "summary.json")
        summary = self.storage._load_json(summary_file, default={})
        summary.update({
            "timestamp": datetime.now().isoformat(),
            "date": val_date,
            "strategy_file": strat_path,
            "universe_file": univ_path,
            "portfolio_value": portfolio_value,
            "data_provider": data_provider,
            "deploy_status": "SUCCESS" if success else "FAILED",
            "deploy_returncode": proc.returncode,
            "deploy_stdout": stdout,
            "deploy_stderr": stderr,
            "trades_count": len([r for r in ticket_records if r.get("action") in ("BUY", "SELL")]),
        })
        self.storage._save_json(summary_file, summary)

        return {
            "status": "success" if success else "error",
            "message": "Stage 2 Live Deployment executed successfully" if success else f"Execution failed with code {proc.returncode}",
            "date": val_date,
            "stdout": stdout,
            "stderr": stderr,
            "health_report": health_data,
            "trading_ticket": ticket_records,
            "account_state": state_data,
            "holdings": effective_holdings,
        }
