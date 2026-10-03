"""Storage and configuration persistence layer for the Live Trading Web Application.

Enforces zero hardcoded paths by prioritizing:
  Explicit Override / CLI > Environment Variable > settings.json > Dynamically Discovered Relative Fallback.
"""

import json
import os
import re
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd


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


try:
    from common.universe import get_stock_name
except ImportError:
    try:
        sys.path.insert(0, find_repo_root())
        from common.universe import get_stock_name
    except Exception:
        def get_stock_name(sym: str, default: Optional[str] = None) -> str:
            return default if default is not None else sym


def normalize_universe_key(key: Optional[str]) -> str:
    """Normalize a universe key for robust comparison across custom universe conventions.
    
    Treats custom universe conventions equivalently:
      'custom_astock_202609_20' <-> 'astock_202609_20_custom' <-> 'astock_202609_20'
    """
    if not key:
        return ""
    k = str(key).strip().lower()
    k = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", k).strip("_")
    if k.startswith("custom_"):
        k = k[7:]
    if k.endswith("_custom"):
        k = k[:-7]
    return k


def keys_match(key_a: Optional[str], key_b: Optional[str]) -> bool:
    """Compare two keys (strategy or universe) for equality, casing, or custom naming equivalence."""
    if key_a is None or key_b is None:
        return key_a == key_b
    ka = str(key_a).strip().lower()
    kb = str(key_b).strip().lower()
    if ka == kb:
        return True
    return normalize_universe_key(ka) == normalize_universe_key(kb)


def get_universe_key_variants(universe_key: Optional[str]) -> List[str]:
    """Generate all common filesystem naming variants for a universe key."""
    if not universe_key:
        return []
    variants: List[str] = [universe_key]
    low = str(universe_key).strip().lower()
    if low not in variants:
        variants.append(low)

    if low.startswith("custom_"):
        slug = low[7:]
        cand_suffix = f"{slug}_custom"
        if cand_suffix not in variants:
            variants.append(cand_suffix)
        if slug not in variants:
            variants.append(slug)
    elif low.endswith("_custom"):
        slug = low[:-7]
        cand_prefix = f"custom_{slug}"
        if cand_prefix not in variants:
            variants.append(cand_prefix)
        if slug not in variants:
            variants.append(slug)
    else:
        cand_prefix = f"custom_{low}"
        cand_suffix = f"{low}_custom"
        if cand_prefix not in variants:
            variants.append(cand_prefix)
        if cand_suffix not in variants:
            variants.append(cand_suffix)

    return variants


def parse_ticket_csv(csv_path: str) -> List[Dict[str, Any]]:
    """Parse live_trading_ticket.csv into a structured list of ticket records if JSON is missing."""
    if not os.path.exists(csv_path):
        return []
    records: List[Dict[str, Any]] = []
    try:
        import csv
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym = row.get("symbol", "").strip()
                name = row.get("name", "").strip()
                if not name or name.lower() in ("custom", "universe", "none", "unknown", "") or name == sym:
                    name = get_stock_name(sym, sym)
                row["name"] = name
                for num_key in ("price", "current_weight", "target_weight", "delta_weight", "trade_value", "trade_value_rmb"):
                    if num_key in row and row[num_key] != "":
                        try:
                            row[num_key] = float(row[num_key])
                        except ValueError:
                            pass
                for int_key in ("current_shares", "target_shares", "delta_shares"):
                    if int_key in row and row[int_key] != "":
                        try:
                            row[int_key] = int(float(row[int_key]))
                        except ValueError:
                            pass
                records.append(row)
    except Exception:
        pass
    return records


def is_run_dir(path: Optional[str]) -> bool:
    """Check if path is a directory containing at least one real run artifact."""
    if not path or not os.path.isdir(path):
        return False
    try:
        files = set(os.listdir(path))
    except OSError:
        return False
    return any(
        f in files
        for f in (
            "stage1_health_report.json",
            "live_trading_ticket.json",
            "live_trading_ticket.csv",
            "summary.json",
            "post_holdings.json",
            "holdings.json",
            "account_state.json",
        )
    )


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

        # Detect local MarketDB availability
        has_marketdb = False
        try:
            from common.financial_api import _resolve_duckdb_path
            has_marketdb = _resolve_duckdb_path() is not None
        except Exception:
            pass

        default_provider_fallback = "marketdb" if has_marketdb else "synthetic"
        self.default_data_provider = saved_settings.get("default_data_provider") or default_provider_fallback
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
    def _extract_strategy_metadata(self, data: Dict[str, Any], filename: str) -> Tuple[str, str]:
        """Extract (strategy_key, human_readable_strategy_name) from strategy JSON metadata."""
        spec_name = None
        if isinstance(data.get("research_strategy_spec"), dict):
            entry = data["research_strategy_spec"].get("entry_data", {})
            if isinstance(entry, dict) and entry.get("name"):
                spec_name = str(entry["name"]).strip()

        name_field = data.get("strategy_name") or data.get("name")
        if name_field and isinstance(name_field, str):
            clean_nf = name_field.strip()
            if clean_nf and not clean_nf.endswith(".json") and clean_nf.lower() != "strategy":
                spec_name = spec_name or clean_nf

        template_name = str(data.get("template_name", "")).strip()
        if not spec_name and template_name:
            spec_name = template_name.replace("_", " ").title()

        clean_file = os.path.splitext(filename)[0]
        clean_file_fmt = clean_file.removesuffix("_strategy").replace("_", " ").title()
        strategy_name = spec_name or clean_file_fmt

        strategy_key = (
            data.get("strategy_key")
            or template_name
            or clean_file.removesuffix("_strategy")
        ).strip().lower()

        return strategy_key, strategy_name

    def resolve_keys(
        self,
        strategy_file: str,
        universe_file: Optional[str] = None,
        custom_symbols: Optional[List[str]] = None,
        custom_name: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Tuple[str, str, str, str]:
        """Resolve (strategy_key, universe_key, strategy_name, universe_name)."""
        # 1. Strategy
        strat_key = strategy_key or "default_strategy"
        strat_name = "Default Strategy"
        if strategy_file:
            strat_path = strategy_file
            if not os.path.isabs(strat_path):
                cand = os.path.join(self.strategy_dir, strategy_file)
                cand_repo = os.path.join(self.repo_root, strategy_file)
                if os.path.exists(cand):
                    strat_path = cand
                elif os.path.exists(cand_repo):
                    strat_path = cand_repo
            strat_data = self._load_json(strat_path, default={}) if os.path.exists(strat_path) else {}
            fname = os.path.basename(strat_path)
            extracted_key, extracted_name = self._extract_strategy_metadata(strat_data, fname)
            if not strategy_key:
                strat_key = extracted_key
            strat_name = extracted_name

        # 2. Universe
        # If custom_name is not provided but custom_symbols match a saved universe, auto-detect custom_name
        if not custom_name and custom_symbols:
            req_set = set(s.strip().upper() for s in custom_symbols if s and s.strip())
            for c in self.list_custom_universes():
                c_set = set(s.strip().upper() for s in c.get("symbols", []) if s and s.strip())
                if c_set and c_set == req_set:
                    custom_name = c.get("name")
                    break

        if custom_name:
            c_slug = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", custom_name).strip("_").lower()
            univ_key = universe_key or f"custom_{c_slug}"
            univ_name = f"★ {custom_name} (Custom)"
        elif custom_symbols and len(custom_symbols) > 0 and (not universe_file or universe_file == "__CUSTOM__"):
            sym_count = len(custom_symbols)
            univ_key = universe_key or f"custom_{sym_count}_symbols"
            univ_name = f"Custom Universe ({sym_count} symbols)"
        elif universe_file and universe_file != "__CUSTOM__":
            clean_u = os.path.basename(universe_file)
            folder = os.path.basename(os.path.dirname(universe_file))
            stem = os.path.splitext(clean_u)[0]
            if folder and folder not in ("universe", "docs", "."):
                raw_key = f"{folder}_{stem}"
            else:
                raw_key = stem
            derived_key = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", raw_key).strip("_").lower()
            univ_key = universe_key or derived_key
            univ_name = f"{folder}/{clean_u}" if folder and folder not in ("universe", "docs", ".") else clean_u
        else:
            univ_key = universe_key or "default_universe"
            univ_name = "Default Universe"

        return strat_key, univ_key, strat_name, univ_name

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
                            strategy_key, strategy_name = self._extract_strategy_metadata(data, f)
                            results.append({
                                "name": strategy_name,
                                "strategy_name": strategy_name,
                                "strategy_key": strategy_key,
                                "filename": f,
                                "path": full_path,
                                "rel_path": rel_path,
                                "template_name": data.get("template_name", ""),
                                "cash_proxy": data.get("params", {}).get("cash_proxy", "BIL"),
                                "description": data.get("description", ""),
                            })
        # Sort by strategy_name
        results.sort(key=lambda x: x["strategy_name"])
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
                        # Derive friendly label and universe_key
                        folder = os.path.basename(os.path.dirname(full_path))
                        name = f"{folder}/{f}" if folder != os.path.basename(self.universe_dir) else f
                        stem = os.path.splitext(f)[0]
                        raw_key = f"{folder}_{stem}" if folder != os.path.basename(self.universe_dir) else stem
                        universe_key = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", raw_key).strip("_").lower()
                        universes.append({
                            "type": "preset",
                            "name": name,
                            "universe_key": universe_key,
                            "filename": f,
                            "path": full_path,
                            "rel_path": rel_path,
                            "symbols": symbols,
                            "count": len(symbols),
                        })

        # 2. Add saved custom universes
        customs = self.list_custom_universes()
        for c in customs:
            c_slug = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]+", "_", c["name"]).strip("_").lower()
            universes.append({
                "type": "custom",
                "name": f"★ {c['name']} (Custom)",
                "universe_key": f"custom_{c_slug}",
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
    def extract_post_run_holdings(self, run_dir: Optional[str]) -> Dict[str, Any]:
        """Extract the resulting post-rebalance holdings from a run directory.
        
        Prioritizes:
          1. post_holdings.json (if explicitly saved)
          2. target_shares in live_trading_ticket.json or live_trading_ticket.csv
          3. holdings.json or current_holdings.json snapshot in run_dir
        """
        if not run_dir or not os.path.exists(run_dir):
            return {}

        # 1. Check if post_holdings.json exists
        post_file = os.path.join(run_dir, "post_holdings.json")
        if os.path.exists(post_file):
            data = self._load_json(post_file, default={})
            if data and isinstance(data, dict):
                return data

        # 2. Check ticket for non-zero target_shares
        ticket_json = os.path.join(run_dir, "live_trading_ticket.json")
        ticket = self._load_json(ticket_json)
        if not ticket:
            csv_path = os.path.join(run_dir, "live_trading_ticket.csv")
            if os.path.exists(csv_path):
                ticket = parse_ticket_csv(csv_path)

        if ticket and isinstance(ticket, list):
            target_holdings = {}
            for r in ticket:
                if isinstance(r, dict):
                    sym = str(r.get("symbol", "")).strip().upper()
                    if not sym or sym == "BIL" or sym.startswith("CASH"):
                        continue
                    ts = r.get("target_shares")
                    if ts is not None:
                        try:
                            s_int = int(float(ts))
                            if s_int > 0:
                                target_holdings[sym] = s_int
                        except (ValueError, TypeError):
                            pass
            if target_holdings:
                return target_holdings

        # 3. Fallback to holdings.json or current_holdings.json in the run dir
        for fname in ("holdings.json", "current_holdings.json"):
            h_path = os.path.join(run_dir, fname)
            if os.path.exists(h_path):
                data = self._load_json(h_path, default={})
                if data and isinstance(data, dict) and data:
                    return data

        return {}

    def get_holdings(
        self,
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if date_str:
            # 1. Check exact run dir for date_str
            run_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str)
            if run_dir:
                post_h = self.extract_post_run_holdings(run_dir)
                if post_h:
                    return post_h
                daily_file = os.path.join(run_dir, "holdings.json")
                if os.path.exists(daily_file):
                    d = self._load_json(daily_file, default={})
                    if d:
                        return d
                curr_file = os.path.join(run_dir, "current_holdings.json")
                if os.path.exists(curr_file):
                    d = self._load_json(curr_file, default={})
                    if d:
                        return d

            # 2. If date_str has no run or run had no holdings, fallback to previous available date run data
            if strategy_key and universe_key:
                prev_run = self.get_previous_run(date_str, strategy_key=strategy_key, universe_key=universe_key)
                if prev_run:
                    prev_path = prev_run.get("path")
                    if prev_path:
                        prev_h = self.extract_post_run_holdings(prev_path)
                        if prev_h:
                            return prev_h
                    if prev_run.get("holdings"):
                        return prev_run["holdings"]

            legacy_file = os.path.join(self.archive_dir, date_str, "holdings.json")
            if os.path.exists(legacy_file):
                d = self._load_json(legacy_file, default={})
                if d:
                    return d

        # If date_str is None, but strategy and universe are provided, fallback to latest run
        if strategy_key and universe_key:
            latest = self.get_latest_run(strategy_key=strategy_key, universe_key=universe_key)
            if latest:
                latest_path = latest.get("path")
                if latest_path:
                    latest_h = self.extract_post_run_holdings(latest_path)
                    if latest_h:
                        return latest_h
                if latest.get("holdings"):
                    return latest["holdings"]

        return self._load_json(self.current_holdings_file, default={})

    def save_holdings(
        self,
        holdings: Dict[str, Any],
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> None:
        self._save_json(self.current_holdings_file, holdings)
        if date_str:
            daily_dir = self.get_run_dir(strategy_key, universe_key, date_str, prefer_existing=True)
            os.makedirs(daily_dir, exist_ok=True)
            self._save_json(os.path.join(daily_dir, "holdings.json"), holdings)

    def get_quotes(
        self,
        symbols: List[str],
        as_of_date: Optional[str] = None,
        data_provider: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch latest quotes and reference market prices for a list of ticker symbols."""
        provider_name = data_provider or self.default_data_provider or "marketdb"
        target_date = as_of_date or date.today().isoformat()
        clean_symbols = [str(s).strip().upper() for s in symbols if s and str(s).strip()]

        quotes: Dict[str, Dict[str, Any]] = {}
        for sym in clean_symbols:
            if sym in ("BIL", "CASH") or sym.startswith("CASH"):
                quotes[sym] = {
                    "symbol": sym,
                    "name": "流动性现金",
                    "price": 1.0,
                    "date": target_date,
                    "source": "cash_proxy",
                }

        needed_symbols = [s for s in clean_symbols if s not in quotes]
        if not needed_symbols:
            return {"quotes": quotes, "data_provider": provider_name, "as_of_date": target_date}

        # 1. Check if run archive exists for target_date or previous run with executed prices
        ticket_prices: Dict[str, float] = {}
        ticket_dates: Dict[str, str] = {}
        run_bundle = None
        if strategy_key and universe_key:
            run_bundle = self.get_run(target_date, strategy_key=strategy_key, universe_key=universe_key)
            if not run_bundle:
                run_bundle = self.get_previous_run(target_date, strategy_key=strategy_key, universe_key=universe_key)

        if run_bundle and run_bundle.get("trading_ticket"):
            bundle_date = run_bundle.get("date", target_date)
            for r in run_bundle["trading_ticket"]:
                if isinstance(r, dict) and r.get("symbol") and r.get("price") is not None:
                    try:
                        p_val = float(r["price"])
                        if p_val > 0:
                            ticket_prices[r["symbol"].upper()] = p_val
                            ticket_dates[r["symbol"].upper()] = bundle_date
                    except (ValueError, TypeError):
                        pass

        # 2. Query market data provider
        start_date = (pd.Timestamp(target_date) - timedelta(days=35)).strftime("%Y-%m-%d")
        loaded_universe: Dict[str, Any] = {}
        try:
            from common.data import load_universe
            loaded_universe = load_universe(
                needed_symbols,
                start=start_date,
                end=target_date,
                interval="1d",
                use_cache=True,
                cache_dir=self.cache_dir,
                provider=provider_name,
            )
        except Exception as exc:
            print(f"[get_quotes] Warning: failed to load from provider '{provider_name}': {exc}")

        for sym in needed_symbols:
            stock_name = get_stock_name(sym, sym)
            has_ticket = sym in ticket_prices
            t_price = ticket_prices.get(sym)
            t_date = ticket_dates.get(sym, "")

            df = loaded_universe.get(sym)
            has_df = df is not None and hasattr(df, "empty") and not df.empty and "Close" in df.columns
            if has_df:
                last_price = float(df["Close"].iloc[-1])
                last_date = df.index[-1].strftime("%Y-%m-%d") if hasattr(df.index[-1], "strftime") else str(df.index[-1])[:10]

                # If provider is synthetic but we have real executed ticket prices, NEVER overwrite with synthetic!
                if has_ticket and provider_name == "synthetic":
                    quotes[sym] = {
                        "symbol": sym,
                        "name": stock_name,
                        "price": t_price,
                        "date": t_date or target_date,
                        "source": "ticket_archive",
                    }
                # If ticket price date is newer than or equal to provider date, prefer real ticket price
                elif has_ticket and t_date and t_date >= last_date:
                    quotes[sym] = {
                        "symbol": sym,
                        "name": stock_name,
                        "price": t_price,
                        "date": t_date,
                        "source": "ticket_archive",
                    }
                else:
                    quotes[sym] = {
                        "symbol": sym,
                        "name": stock_name,
                        "price": last_price,
                        "date": last_date,
                        "source": provider_name,
                    }
            elif has_ticket:
                quotes[sym] = {
                    "symbol": sym,
                    "name": stock_name,
                    "price": t_price,
                    "date": t_date or target_date,
                    "source": "ticket_archive",
                }
            else:
                quotes[sym] = {
                    "symbol": sym,
                    "name": stock_name,
                    "price": None,
                    "date": None,
                    "source": "not_found",
                }

        return {"quotes": quotes, "data_provider": provider_name, "as_of_date": target_date}

    # --------------------------------------------------------------------------
    # Account State Management (Global + Daily Archive)
    # --------------------------------------------------------------------------
    def get_account_state(
        self,
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if date_str:
            # 1. Check exact run dir for date_str
            run_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str)
            if run_dir:
                daily_file = os.path.join(run_dir, "account_state.json")
                if os.path.exists(daily_file):
                    d = self._load_json(daily_file, default={})
                    if d:
                        return d

            # 2. If no exact run on date_str, fallback to previous available date run
            if strategy_key and universe_key:
                prev_run = self.get_previous_run(date_str, strategy_key=strategy_key, universe_key=universe_key)
                if prev_run:
                    if prev_run.get("account_state"):
                        return prev_run["account_state"]
                    elif prev_run.get("path"):
                        prev_file = os.path.join(prev_run["path"], "account_state.json")
                        if os.path.exists(prev_file):
                            return self._load_json(prev_file, default={})

            legacy_file = os.path.join(self.archive_dir, date_str, "account_state.json")
            if os.path.exists(legacy_file):
                d = self._load_json(legacy_file, default={})
                if d:
                    return d

        # If date_str is None, but strategy and universe are provided, fallback to latest run
        if strategy_key and universe_key:
            latest = self.get_latest_run(strategy_key=strategy_key, universe_key=universe_key)
            if latest and latest.get("account_state"):
                return latest["account_state"]

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

    def save_account_state(
        self,
        state: Dict[str, Any],
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> None:
        self._save_json(self.account_state_file, state)
        if date_str:
            daily_dir = self.get_run_dir(strategy_key, universe_key, date_str, prefer_existing=True)
            os.makedirs(daily_dir, exist_ok=True)
            self._save_json(os.path.join(daily_dir, "account_state.json"), state)

    # --------------------------------------------------------------------------
    # Daily Archive Runs Management (Keyed by Strategy and Universe)
    # --------------------------------------------------------------------------
    def find_existing_run_dir(
        self,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        date_str: Optional[str] = None,
    ) -> Optional[str]:
        """Find an existing run directory on disk, taking into account universe key variants and case insensitivity."""
        def _matches(p: str) -> bool:
            if not os.path.exists(p):
                return False
            if date_str:
                return is_run_dir(p)
            return os.path.isdir(p)

        if not strategy_key or not universe_key:
            if date_str:
                flat_legacy = os.path.join(self.archive_dir, date_str)
                if _matches(flat_legacy):
                    return flat_legacy
            return None

        # 1. Direct candidate
        cand = self.get_run_dir(strategy_key, universe_key, date_str, prefer_existing=False)
        if _matches(cand):
            return cand

        # 2. Check universe key variants under strategy_key
        for u_var in get_universe_key_variants(universe_key):
            cand_var = self.get_run_dir(strategy_key, u_var, date_str, prefer_existing=False)
            if _matches(cand_var):
                return cand_var

        # 3. Check within strategy folder for any subfolder whose key matches
        strat_dir = os.path.join(self.archive_dir, strategy_key)
        if os.path.isdir(strat_dir):
            for u_entry in os.listdir(strat_dir):
                if keys_match(u_entry, universe_key):
                    cand_entry = os.path.join(strat_dir, u_entry, date_str) if date_str else os.path.join(strat_dir, u_entry)
                    if _matches(cand_entry):
                        return cand_entry

        # 4. Search across all strategy folders in archive_dir
        if os.path.isdir(self.archive_dir):
            for s_entry in os.listdir(self.archive_dir):
                if keys_match(s_entry, strategy_key):
                    s_path = os.path.join(self.archive_dir, s_entry)
                    if os.path.isdir(s_path):
                        for u_entry in os.listdir(s_path):
                            if keys_match(u_entry, universe_key):
                                cand_entry = os.path.join(s_path, u_entry, date_str) if date_str else os.path.join(s_path, u_entry)
                                if _matches(cand_entry):
                                    return cand_entry

        return None

    def get_run_dir(
        self,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        date_str: Optional[str] = None,
        prefer_existing: bool = True,
    ) -> str:
        """Construct hierarchical archive directory: <archive_dir>/<strategy_key>/<universe_key>/<date_str>.
        If prefer_existing is True and a matching strategy/universe folder already exists on disk,
        reuse that folder to prevent directory fragmentation.
        """
        if prefer_existing and strategy_key and universe_key:
            existing_univ_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str=None)
            if existing_univ_dir and os.path.isdir(existing_univ_dir):
                if date_str:
                    return os.path.join(existing_univ_dir, date_str)
                return existing_univ_dir

        parts = [self.archive_dir]
        if strategy_key and universe_key:
            parts.append(strategy_key)
            parts.append(universe_key)
        if date_str:
            parts.append(date_str)
        return os.path.join(*parts)

    def save_daily_run(
        self,
        date_str: str,
        health_data: Optional[Dict[str, Any]] = None,
        ticket_data: Optional[List[Dict[str, Any]]] = None,
        csv_content: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
        holdings: Optional[Dict[str, Any]] = None,
        account_state: Optional[Dict[str, Any]] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        strategy_name: Optional[str] = None,
        universe_name: Optional[str] = None,
    ) -> str:
        """Atomically saves all artifacts for date_str in archive_dir/<strategy_key>/<universe_key>/<date_str>/."""
        run_dir = self.get_run_dir(strategy_key, universe_key, date_str, prefer_existing=True)
        os.makedirs(run_dir, exist_ok=True)

        if summary is None:
            summary = {}
        if strategy_key:
            summary["strategy_key"] = strategy_key
        if universe_key:
            summary["universe_key"] = universe_key
        if strategy_name:
            summary["strategy_name"] = strategy_name
        if universe_name:
            summary["universe_name"] = universe_name
        summary["date"] = date_str

        if health_data is not None:
            self._save_json(os.path.join(run_dir, "stage1_health_report.json"), health_data)
        if ticket_data is not None:
            if isinstance(ticket_data, list):
                for r in ticket_data:
                    if isinstance(r, dict):
                        name_val = str(r.get("name", "")).strip()
                        if not name_val or name_val.lower() in ("custom", "universe", "none", "unknown") or name_val == r.get("symbol"):
                            r["name"] = get_stock_name(r.get("symbol", ""), r.get("symbol", ""))
            self._save_json(os.path.join(run_dir, "live_trading_ticket.json"), ticket_data)
        if csv_content is not None:
            with open(os.path.join(run_dir, "live_trading_ticket.csv"), "w", encoding="utf-8") as f:
                f.write(csv_content)
        if summary:
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

    def list_runs(
        self,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
        require_both: bool = False,
    ) -> List[Dict[str, Any]]:
        """List archived runs, optionally filtered strictly by strategy and universe."""
        if require_both and (not strategy_key or not universe_key):
            return []

        if not os.path.exists(self.archive_dir):
            return []

        runs = []
        # Walk archive_dir to discover run directories (both 3-level and legacy 1-level)
        for root, dirs, files in os.walk(self.archive_dir):
            if (
                "stage1_health_report.json" in files
                or "live_trading_ticket.json" in files
                or "summary.json" in files
                or "live_trading_ticket.csv" in files
            ):
                health_path = os.path.join(root, "stage1_health_report.json")
                ticket_path = os.path.join(root, "live_trading_ticket.json")
                csv_path = os.path.join(root, "live_trading_ticket.csv")
                summary_path = os.path.join(root, "summary.json")

                health_rep = self._load_json(health_path, default={})
                ticket_rep = self._load_json(ticket_path, default=[])
                summary_rep = self._load_json(summary_path, default={})

                if not ticket_rep and os.path.exists(csv_path):
                    ticket_rep = parse_ticket_csv(csv_path)

                # Determine date from summary or directory basename
                date_val = summary_rep.get("date") or os.path.basename(root)

                # Determine strategy and universe keys
                r_strat_key = summary_rep.get("strategy_key")
                r_univ_key = summary_rep.get("universe_key")
                r_strat_name = summary_rep.get("strategy_name")
                r_univ_name = summary_rep.get("universe_name")

                # If missing from summary, deduce from relative path or strategy_file
                rel_parts = os.path.relpath(root, self.archive_dir).split(os.sep)
                if len(rel_parts) >= 3:
                    if not r_strat_key:
                        r_strat_key = rel_parts[0]
                    if not r_univ_key:
                        r_univ_key = rel_parts[1]
                else:
                    # Legacy flat runs
                    strat_file = summary_rep.get("strategy_file") or ""
                    univ_file = summary_rep.get("universe_file") or ""
                    sk, uk, sn, un = self.resolve_keys(strat_file, univ_file)
                    if not r_strat_key:
                        r_strat_key = sk
                    if not r_univ_key:
                        r_univ_key = uk
                    if not r_strat_name:
                        r_strat_name = sn
                    if not r_univ_name:
                        r_univ_name = un

                if not r_univ_name and r_univ_key:
                    for c in self.list_custom_universes():
                        if keys_match(c.get("name", ""), r_univ_key):
                            r_univ_name = f"★ {c.get('name')} (Custom)"
                            break
                    if not r_univ_name:
                        r_univ_name = (r_univ_key or "Universe").replace("_", " ")

                if not r_strat_name:
                    r_strat_name = (r_strat_key or "Strategy").replace("_", " ").title()

                # Apply filters using keys_match
                if strategy_key and r_strat_key and not keys_match(strategy_key, r_strat_key):
                    continue
                if universe_key and r_univ_key and not keys_match(universe_key, r_univ_key):
                    continue

                directive = health_rep.get("directive") or health_rep.get("gate_code", "UNKNOWN")
                tier = health_rep.get("circuit_breaker_tier") or health_rep.get("portfolio_health", {}).get("tier", "NORMAL")
                trade_count = len([x for x in ticket_rep if isinstance(x, dict) and x.get("action") in ("BUY", "SELL")])

                runs.append({
                    "date": date_val,
                    "path": root,
                    "strategy_key": r_strat_key or "",
                    "strategy_name": r_strat_name,
                    "universe_key": r_univ_key or "",
                    "universe_name": r_univ_name,
                    "has_health": os.path.exists(health_path),
                    "has_ticket": os.path.exists(ticket_path) or os.path.exists(csv_path),
                    "directive": directive,
                    "tier": tier,
                    "trade_count": trade_count,
                    "timestamp": summary_rep.get("timestamp", ""),
                    "strategy": summary_rep.get("strategy_file", ""),
                    "universe": summary_rep.get("universe_file", ""),
                })

        # Sort by date descending, then timestamp descending
        runs.sort(key=lambda x: (x.get("date", ""), x.get("timestamp", "")), reverse=True)
        return runs

    def get_latest_run(
        self,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve latest run bundle for a specific strategy and universe."""
        runs = self.list_runs(strategy_key=strategy_key, universe_key=universe_key, require_both=True)
        if not runs:
            return None
        top_run = runs[0]
        return self.get_run(
            top_run["date"],
            strategy_key=top_run.get("strategy_key") or strategy_key,
            universe_key=top_run.get("universe_key") or universe_key,
        )

    def get_previous_run(
        self,
        date_str: Optional[str] = None,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve the latest archived run strictly before date_str (or latest available if date_str is None)."""
        runs = self.list_runs(strategy_key=strategy_key, universe_key=universe_key, require_both=True)
        if not runs and (strategy_key or universe_key):
            runs = self.list_runs(strategy_key=strategy_key, universe_key=universe_key, require_both=False)
        if not runs:
            return None

        if date_str:
            earlier_runs = [r for r in runs if r.get("date", "") < date_str]
            if not earlier_runs:
                return None
            chosen = earlier_runs[0]
        else:
            chosen = runs[0]

        return self.get_run(
            chosen["date"],
            strategy_key=chosen.get("strategy_key") or strategy_key,
            universe_key=chosen.get("universe_key") or universe_key,
        )

    def get_run(
        self,
        date_str: str,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve complete archived artifacts for a date, optionally matching strategy & universe."""
        run_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str)
        if not run_dir:
            # Search list_runs
            matching = [
                r for r in self.list_runs(strategy_key=strategy_key, universe_key=universe_key)
                if r["date"] == date_str
            ]
            if matching:
                run_dir = matching[0]["path"]
            else:
                legacy = os.path.join(self.archive_dir, date_str)
                if is_run_dir(legacy):
                    run_dir = legacy

        if not run_dir or not is_run_dir(run_dir):
            return None

        summary_data = self._load_json(os.path.join(run_dir, "summary.json"), default={})
        ticket = self._load_json(os.path.join(run_dir, "live_trading_ticket.json"))
        if not ticket:
            csv_path = os.path.join(run_dir, "live_trading_ticket.csv")
            if os.path.exists(csv_path):
                ticket = parse_ticket_csv(csv_path)

        if ticket and isinstance(ticket, list):
            for r in ticket:
                if isinstance(r, dict):
                    name_val = str(r.get("name", "")).strip()
                    if not name_val or name_val.lower() in ("custom", "universe", "none", "unknown") or name_val == r.get("symbol"):
                        r["name"] = get_stock_name(r.get("symbol", ""), r.get("symbol", ""))

        strat_k = summary_data.get("strategy_key", strategy_key or "")
        univ_k = universe_key or summary_data.get("universe_key", "")
        strat_n = summary_data.get("strategy_name", "")
        univ_n = summary_data.get("universe_name", "")

        # Holdings extraction: holdings.json first, then current_holdings.json, then post_holdings, then previous run
        holdings_data = self._load_json(os.path.join(run_dir, "holdings.json"))
        if not holdings_data:
            holdings_data = self._load_json(os.path.join(run_dir, "current_holdings.json"))
        if not holdings_data:
            holdings_data = self.extract_post_run_holdings(run_dir)
        if not holdings_data and strat_k and univ_k:
            prev_run = self.get_previous_run(date_str, strategy_key=strat_k, universe_key=univ_k)
            if prev_run:
                holdings_data = self.extract_post_run_holdings(prev_run.get("path")) or prev_run.get("holdings", {})

        account_state = self._load_json(os.path.join(run_dir, "account_state.json"))
        if not account_state and strat_k and univ_k:
            prev_run = self.get_previous_run(date_str, strategy_key=strat_k, universe_key=univ_k)
            if prev_run:
                account_state = prev_run.get("account_state")

        # If summary didn't have nice names, resolve from custom universes / strategies
        if not univ_n and univ_k:
            for c in self.list_custom_universes():
                if keys_match(c.get("name", ""), univ_k):
                    univ_n = f"★ {c.get('name')} (Custom)"
                    break
            if not univ_n:
                univ_n = univ_k.replace("_", " ").title()

        if not strat_n and strat_k:
            strat_n = strat_k.replace("_", " ").title()

        prices_map: Dict[str, float] = {}
        if ticket and isinstance(ticket, list):
            for r in ticket:
                if isinstance(r, dict) and r.get("symbol") and r.get("price") is not None:
                    try:
                        p_val = float(r["price"])
                        if p_val > 0:
                            prices_map[str(r["symbol"]).upper()] = p_val
                    except (ValueError, TypeError):
                        pass

        return {
            "date": date_str,
            "path": run_dir,
            "strategy_key": strat_k,
            "strategy_name": strat_n,
            "universe_key": univ_k,
            "universe_name": univ_n,
            "health_report": self._load_json(os.path.join(run_dir, "stage1_health_report.json")),
            "trading_ticket": ticket,
            "summary": summary_data,
            "holdings": holdings_data or {},
            "prices": prices_map,
            "account_state": account_state,
            "has_csv": os.path.exists(os.path.join(run_dir, "live_trading_ticket.csv")),
        }

    def get_run_csv(
        self,
        date_str: str,
        strategy_key: Optional[str] = None,
        universe_key: Optional[str] = None,
    ) -> Optional[str]:
        run_dir = self.find_existing_run_dir(strategy_key, universe_key, date_str)
        if not run_dir:
            matching = [
                r for r in self.list_runs(strategy_key=strategy_key, universe_key=universe_key)
                if r["date"] == date_str
            ]
            if matching:
                run_dir = matching[0]["path"]
            else:
                legacy = os.path.join(self.archive_dir, date_str)
                if os.path.exists(legacy):
                    run_dir = legacy

        if run_dir:
            csv_file = os.path.join(run_dir, "live_trading_ticket.csv")
            if os.path.exists(csv_file):
                with open(csv_file, "r", encoding="utf-8") as f:
                    return f.read()
        return None

