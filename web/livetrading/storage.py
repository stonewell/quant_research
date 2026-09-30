"""Storage and configuration persistence layer for the Live Trading Web Application.

Enforces zero hardcoded paths by prioritizing:
  Explicit Override / CLI > Environment Variable > settings.json > Dynamically Discovered Relative Fallback.
"""

import json
import os
import sys
from datetime import date
from typing import Any, Dict, List, Optional


def find_repo_root() -> str:
    """Dynamically discover the quantitative workspace repository root."""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if os.path.isfile(os.path.join(d, "AGENTS.md")) or os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # Fallback to two levels up if marker not found
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class StorageManager:
    """Manages paths, settings, custom universes, holdings, account states, and daily archives."""

    def __init__(
        self,
        repo_root: Optional[str] = None,
        data_dir: Optional[str] = None,
        archive_dir: Optional[str] = None,
        strategy_dir: Optional[str] = None,
        universe_dir: Optional[str] = None,
        cache_dir: Optional[str] = None,
        stage1_script: Optional[str] = None,
        stage2_script: Optional[str] = None,
        python_executable: Optional[str] = None,
    ):
        # 1. Resolve repo_root
        self.repo_root = (
            repo_root
            or os.environ.get("LIVETRADING_REPO_ROOT")
            or find_repo_root()
        )
        self.repo_root = os.path.abspath(self.repo_root)

        # 2. Base data_dir
        self.data_dir = os.path.abspath(
            data_dir
            or os.environ.get("LIVETRADING_DATA_DIR")
            or os.path.join(self.repo_root, "web", "livetrading", "data")
        )
        os.makedirs(self.data_dir, exist_ok=True)

        # 3. Load persistent settings from settings.json if present
        self.settings_file = os.path.join(self.data_dir, "settings.json")
        saved_settings = self._load_json(self.settings_file, default={})

        # 4. Resolve remaining paths with hierarchy: Parameter > Env > Settings > Dynamic Default
        self.archive_dir = os.path.abspath(
            archive_dir
            or os.environ.get("LIVETRADING_ARCHIVE_DIR")
            or saved_settings.get("archive_dir")
            or os.path.join(self.data_dir, "runs")
        )
        os.makedirs(self.archive_dir, exist_ok=True)

        self.strategy_dir = os.path.abspath(
            strategy_dir
            or os.environ.get("LIVETRADING_STRATEGY_DIR")
            or saved_settings.get("strategy_dir")
            or os.path.join(self.repo_root, "pipeline", "research_strategy", "results", "strategy_dumps")
        )

        self.universe_dir = os.path.abspath(
            universe_dir
            or os.environ.get("LIVETRADING_UNIVERSE_DIR")
            or saved_settings.get("universe_dir")
            or os.path.join(self.repo_root, "docs", "universe")
        )

        self.cache_dir = os.path.abspath(
            cache_dir
            or os.environ.get("LIVETRADING_CACHE_DIR")
            or saved_settings.get("cache_dir")
            or os.path.join(self.repo_root, "data")
        )

        self.stage1_script = os.path.abspath(
            stage1_script
            or os.environ.get("LIVETRADING_STAGE1_SCRIPT")
            or saved_settings.get("stage1_script")
            or os.path.join(self.repo_root, "scripts", "check_live_portfolio_health.py")
        )

        self.stage2_script = os.path.abspath(
            stage2_script
            or os.environ.get("LIVETRADING_STAGE2_SCRIPT")
            or saved_settings.get("stage2_script")
            or os.path.join(self.repo_root, "scripts", "run_live_four_state_blend.py")
        )

        resolved_py = (
            python_executable
            or os.environ.get("LIVETRADING_PYTHON_BIN")
            or saved_settings.get("python_executable")
        )
        if not resolved_py:
            pipeline_py = os.path.join(self.repo_root, "pipeline", ".venv", "bin", "python")
            web_py = os.path.join(self.repo_root, "web", "livetrading", ".venv", "bin", "python")
            if os.path.exists(pipeline_py):
                resolved_py = pipeline_py
            elif os.path.exists(web_py):
                resolved_py = web_py
            else:
                resolved_py = sys.executable

        self.python_executable = os.path.abspath(resolved_py)

        self.default_data_provider = saved_settings.get("default_data_provider", "synthetic")
        self.default_portfolio_value = float(saved_settings.get("default_portfolio_value", 100000.0))

        # Files in data_dir
        self.custom_universes_file = os.path.join(self.data_dir, "custom_universes.json")
        self.current_holdings_file = os.path.join(self.data_dir, "current_holdings.json")
        self.account_state_file = os.path.join(self.data_dir, "account_state.json")

    def _load_json(self, path: str, default: Any = None) -> Any:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return default
        return default

    def _save_json(self, path: str, data: Any) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    # --------------------------------------------------------------------------
    # Settings Management
    # --------------------------------------------------------------------------
    def get_settings(self) -> Dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "strategy_dir": self.strategy_dir,
            "universe_dir": self.universe_dir,
            "data_dir": self.data_dir,
            "archive_dir": self.archive_dir,
            "cache_dir": self.cache_dir,
            "stage1_script": self.stage1_script,
            "stage2_script": self.stage2_script,
            "python_executable": self.python_executable,
            "default_data_provider": self.default_data_provider,
            "default_portfolio_value": self.default_portfolio_value,
        }

    def update_settings(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_settings()
        for k, v in new_settings.items():
            if v is not None and k in current:
                setattr(self, k, v)
                current[k] = v
        self._save_json(self.settings_file, current)
        return current

    # --------------------------------------------------------------------------
    # Custom Universes Management
    # --------------------------------------------------------------------------
    def list_custom_universes(self) -> List[Dict[str, Any]]:
        raw = self._load_json(self.custom_universes_file, default=[])
        if isinstance(raw, dict):
            # In case saved as a map name -> obj
            return list(raw.values())
        return raw if isinstance(raw, list) else []

    def save_custom_universe(self, name: str, symbols: List[str], description: str = "") -> Dict[str, Any]:
        universes = self.list_custom_universes()
        # Clean symbols
        cleaned_symbols = [s.strip().upper() for s in symbols if s and s.strip()]
        new_entry = {
            "name": name.strip(),
            "description": description.strip(),
            "symbols": cleaned_symbols,
            "count": len(cleaned_symbols),
        }
        # Replace if exists, else append
        updated = [u for u in universes if u.get("name") != new_entry["name"]]
        updated.append(new_entry)
        self._save_json(self.custom_universes_file, updated)
        return new_entry

    def delete_custom_universe(self, name: str) -> bool:
        universes = self.list_custom_universes()
        filtered = [u for u in universes if u.get("name") != name]
        if len(filtered) < len(universes):
            self._save_json(self.custom_universes_file, filtered)
            return True
        return False

    # --------------------------------------------------------------------------
    # Discovery: Strategies & Universes
    # --------------------------------------------------------------------------
    def discover_strategies(self) -> List[Dict[str, Any]]:
        results = []
        dirs_to_scan = [self.strategy_dir]
        # Also include generator results if present and different
        gen_dir = os.path.join(self.repo_root, "pipeline", "strategy_generator", "results")
        if os.path.isdir(gen_dir) and gen_dir not in dirs_to_scan:
            dirs_to_scan.append(gen_dir)

        for d in dirs_to_scan:
            if not os.path.exists(d):
                continue
            for root, _, files in os.walk(d):
                for f in files:
                    if f.endswith(".json") and "strategy" in f.lower():
                        full_path = os.path.join(root, f)
                        rel_path = os.path.relpath(full_path, self.repo_root)
                        data = self._load_json(full_path)
                        if isinstance(data, dict) and "template_name" in data:
                            results.append({
                                "name": os.path.splitext(f)[0],
                                "filename": f,
                                "path": full_path,
                                "rel_path": rel_path,
                                "template_name": data.get("template_name", ""),
                                "cash_proxy": data.get("params", {}).get("cash_proxy", "BIL"),
                                "description": data.get("description", ""),
                            })
        # Sort by filename
        results.sort(key=lambda x: x["filename"])
        return results

    def discover_universes(self) -> List[Dict[str, Any]]:
        universes = []
        # 1. Pre-defined from universe_dir
        if os.path.exists(self.universe_dir):
            for root, _, files in os.walk(self.universe_dir):
                for f in files:
                    if f.endswith(".txt"):
                        full_path = os.path.join(root, f)
                        rel_path = os.path.relpath(full_path, self.repo_root)
                        # Count symbols
                        symbols = []
                        try:
                            with open(full_path, "r", encoding="utf-8") as ufile:
                                for line in ufile:
                                    s = line.strip()
                                    if s and not s.startswith("#"):
                                        sym = s.replace(",", " ").split()[0].upper()
                                        symbols.append(sym)
                        except Exception:
                            symbols = []
                        # Derive friendly label from relative parent path
                        folder = os.path.basename(os.path.dirname(full_path))
                        name = f"{folder}/{f}" if folder != os.path.basename(self.universe_dir) else f
                        universes.append({
                            "type": "preset",
                            "name": name,
                            "filename": f,
                            "path": full_path,
                            "rel_path": rel_path,
                            "symbols": symbols,
                            "count": len(symbols),
                        })

        # 2. Add saved custom universes
        customs = self.list_custom_universes()
        for c in customs:
            universes.append({
                "type": "custom",
                "name": f"★ {c['name']} (Custom)",
                "custom_name": c["name"],
                "description": c.get("description", ""),
                "symbols": c.get("symbols", []),
                "count": len(c.get("symbols", [])),
                "path": None,
                "rel_path": None,
            })

        return universes

    # --------------------------------------------------------------------------
    # Holdings Management (Global + Daily Archive)
    # --------------------------------------------------------------------------
    def get_holdings(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        if date_str:
            daily_file = os.path.join(self.archive_dir, date_str, "holdings.json")
            if os.path.exists(daily_file):
                return self._load_json(daily_file, default={})
        return self._load_json(self.current_holdings_file, default={})

    def save_holdings(self, holdings: Dict[str, Any], date_str: Optional[str] = None) -> None:
        self._save_json(self.current_holdings_file, holdings)
        if date_str:
            daily_file = os.path.join(self.archive_dir, date_str, "holdings.json")
            self._save_json(daily_file, holdings)

    # --------------------------------------------------------------------------
    # Account State Management (Global + Daily Archive)
    # --------------------------------------------------------------------------
    def get_account_state(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        if date_str:
            daily_file = os.path.join(self.archive_dir, date_str, "account_state.json")
            if os.path.exists(daily_file):
                return self._load_json(daily_file, default={})
        return self._load_json(
            self.account_state_file,
            default={
                "as_of_date": None,
                "current_nav": 100000.0,
                "peak_nav": 100000.0,
                "circuit_breaker_tier": "NORMAL",
                "circuit_breaker_label": "🟢 NORMAL",
                "linear_equity_scale": 1.0,
                "tier1_consecutive_bars": 0,
                "tier3_consecutive_bars": 0,
                "freeze_remaining_bars": 0,
                "nav_history_10d": [],
                "holdings": {},
            },
        )

    def save_account_state(self, state: Dict[str, Any], date_str: Optional[str] = None) -> None:
        self._save_json(self.account_state_file, state)
        if date_str:
            daily_file = os.path.join(self.archive_dir, date_str, "account_state.json")
            self._save_json(daily_file, state)

    # --------------------------------------------------------------------------
    # Daily Archive Runs Management
    # --------------------------------------------------------------------------
    def save_daily_run(
        self,
        date_str: str,
        health_data: Optional[Dict[str, Any]] = None,
        ticket_data: Optional[List[Dict[str, Any]]] = None,
        csv_content: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
        holdings: Optional[Dict[str, Any]] = None,
        account_state: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Atomically saves all artifacts for date_str in archive_dir/date_str/."""
        run_dir = os.path.join(self.archive_dir, date_str)
        os.makedirs(run_dir, exist_ok=True)

        if health_data is not None:
            self._save_json(os.path.join(run_dir, "stage1_health_report.json"), health_data)
        if ticket_data is not None:
            self._save_json(os.path.join(run_dir, "live_trading_ticket.json"), ticket_data)
        if csv_content is not None:
            with open(os.path.join(run_dir, "live_trading_ticket.csv"), "w", encoding="utf-8") as f:
                f.write(csv_content)
        if summary is not None:
            self._save_json(os.path.join(run_dir, "summary.json"), summary)
        if holdings is not None:
            self._save_json(os.path.join(run_dir, "holdings.json"), holdings)
            # also update global
            self._save_json(self.current_holdings_file, holdings)
        if account_state is not None:
            self._save_json(os.path.join(run_dir, "account_state.json"), account_state)
            # also update global
            self._save_json(self.account_state_file, account_state)

        return run_dir

    def list_runs(self) -> List[Dict[str, Any]]:
        """List all archived run dates with high-level summaries."""
        if not os.path.exists(self.archive_dir):
            return []

        runs = []
        for d in sorted(os.listdir(self.archive_dir), reverse=True):
            sub = os.path.join(self.archive_dir, d)
            if not os.path.isdir(sub):
                continue

            health_path = os.path.join(sub, "stage1_health_report.json")
            ticket_path = os.path.join(sub, "live_trading_ticket.json")
            summary_path = os.path.join(sub, "summary.json")

            health_rep = self._load_json(health_path, default={})
            ticket_rep = self._load_json(ticket_path, default=[])
            summary_rep = self._load_json(summary_path, default={})

            directive = health_rep.get("directive") or health_rep.get("gate_code", "UNKNOWN")
            tier = health_rep.get("circuit_breaker_tier") or health_rep.get("portfolio_health", {}).get("tier", "NORMAL")
            trade_count = len([x for x in ticket_rep if x.get("action") in ("BUY", "SELL")])

            runs.append({
                "date": d,
                "path": sub,
                "has_health": os.path.exists(health_path),
                "has_ticket": os.path.exists(ticket_path),
                "directive": directive,
                "tier": tier,
                "trade_count": trade_count,
                "timestamp": summary_rep.get("timestamp", ""),
                "strategy": summary_rep.get("strategy_file", ""),
                "universe": summary_rep.get("universe_file", ""),
            })

        return runs

    def get_run(self, date_str: str) -> Optional[Dict[str, Any]]:
        """Retrieve complete archived artifacts for a date."""
        run_dir = os.path.join(self.archive_dir, date_str)
        if not os.path.exists(run_dir):
            return None

        return {
            "date": date_str,
            "health_report": self._load_json(os.path.join(run_dir, "stage1_health_report.json")),
            "trading_ticket": self._load_json(os.path.join(run_dir, "live_trading_ticket.json")),
            "summary": self._load_json(os.path.join(run_dir, "summary.json")),
            "holdings": self._load_json(os.path.join(run_dir, "holdings.json")),
            "account_state": self._load_json(os.path.join(run_dir, "account_state.json")),
            "has_csv": os.path.exists(os.path.join(run_dir, "live_trading_ticket.csv")),
        }

    def get_run_csv(self, date_str: str) -> Optional[str]:
        csv_file = os.path.join(self.archive_dir, date_str, "live_trading_ticket.csv")
        if os.path.exists(csv_file):
            with open(csv_file, "r", encoding="utf-8") as f:
                return f.read()
        return None
