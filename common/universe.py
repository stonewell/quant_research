"""Shared Universe Provider framework supporting static symbol lists, universe files,
and dynamic code universe providers across all quant projects in this workspace.
"""

from abc import ABC, abstractmethod
import importlib
import importlib.util
import json
import os
import sys
from typing import Callable, Dict, List, Optional, Type, Union
import warnings


class BaseUniverseProvider(ABC):
    """Abstract base class for all universe providers."""

    @abstractmethod
    def get_symbols(self, **kwargs) -> List[str]:
        """Returns a deduplicated list of ticker symbol strings."""
        pass


class StaticUniverseProvider(BaseUniverseProvider):
    """Universe provider returning a fixed static list of symbols."""

    def __init__(self, symbols: Union[List[str], str, None] = None):
        if symbols is None:
            self.symbols = []
        elif isinstance(symbols, str):
            self.symbols = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        else:
            self.symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]

    def get_symbols(self, **kwargs) -> List[str]:
        # Deduplicate preserving order
        seen = set()
        out = []
        for s in self.symbols:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out


class FileUniverseProvider(BaseUniverseProvider):
    """Universe provider loading symbols from a JSON, CSV, or text file."""

    def __init__(self, file_path: str):
        self.file_path = file_path

    def get_symbols(self, **kwargs) -> List[str]:
        if not os.path.exists(self.file_path):
            raise FileNotFoundError(f"Universe file not found at '{self.file_path}'")

        symbols = []
        if self.file_path.endswith(".json"):
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, list):
                raw_list = data
            elif isinstance(data, dict):
                raw_list = (
                    data.get("basket")
                    or data.get("symbols")
                    or data.get("universe")
                    or data.get("tickers")
                    or []
                )
            else:
                raw_list = []

            if not isinstance(raw_list, list):
                raise ValueError(
                    f"Universe file '{self.file_path}': 'basket'/'symbols'/'universe'/'tickers' "
                    f"value must be a JSON list, got {type(raw_list).__name__}."
                )

            skipped = []
            for item in raw_list:
                if isinstance(item, str):
                    symbols.append(item)
                elif isinstance(item, dict) and "symbol" in item:
                    symbols.append(str(item["symbol"]))
                elif isinstance(item, dict) and "ticker" in item:
                    symbols.append(str(item["ticker"]))
                else:
                    skipped.append(item)

            if skipped:
                preview = skipped[:5]
                suffix = "..." if len(skipped) > 5 else ""
                warnings.warn(
                    f"Universe file '{self.file_path}': ignored {len(skipped)} malformed "
                    f"entr{'y' if len(skipped) == 1 else 'ies'}: {preview}{suffix}"
                )
        else:
            with open(self.file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    tokens = [t.strip().upper() for t in line.replace(",", " ").split() if t.strip()]
                    symbols.extend(tokens)

        # Deduplicate preserving order
        seen = set()
        out = []
        for s in symbols:
            s_clean = str(s).strip().upper()
            if s_clean and s_clean not in seen:
                seen.add(s_clean)
                out.append(s_clean)

        if not out:
            raise ValueError(f"Universe file '{self.file_path}' resolved to zero valid ticker symbols.")

        return out


def _is_module_attr_split(parts: List[str]) -> bool:
    """True if a `target_str.rsplit(':', 1)` result looks like a genuine
    'module_or_path:attr' split rather than a Windows drive-letter colon
    embedded in a plain path (e.g. 'C:\\foo\\bar.json' rsplits into
    ['C', '\\foo\\bar.json'], which is NOT a module:attr specifier)."""
    if "\\" in parts[1] or "/" in parts[1] or parts[1].endswith(".py"):
        return False
    if len(parts[0]) == 1 and os.path.exists(":".join(parts)):
        return False
    return True


def _looks_like_code_specifier(target_str: str) -> bool:
    """True if target_str looks like a CodeUniverseProvider target: a bare
    '.py' script path (relying on the 'get_universe' default attr), or an
    explicit 'module_or_path:attr' specifier."""
    if target_str.endswith(".py"):
        return True
    if ":" in target_str:
        return _is_module_attr_split(target_str.rsplit(":", 1))
    return False


def _parse_module_specifier(target_str: str, default_attr: Optional[str] = None):
    target_str = target_str.strip()
    path_or_module = target_str
    attr_name = default_attr

    if ":" in target_str:
        parts = target_str.rsplit(":", 1)
        if _is_module_attr_split(parts):
            path_or_module, attr_name = parts[0], parts[1]

    return path_or_module, attr_name


class CodeUniverseProvider(BaseUniverseProvider):
    """Universe provider executing a Python module function or script to generate a universe by code.

    Supports target syntax:
    - 'package.module:function_or_class'
    - 'path/to/script.py:function_or_class'
    """

    def __init__(self, target: Union[str, Callable]):
        self.target = target

    def get_symbols(self, **kwargs) -> List[str]:
        if callable(self.target):
            fn = self.target
        elif isinstance(self.target, str):
            path_or_module, func_name = _parse_module_specifier(self.target, default_attr="get_universe")

            if os.path.exists(path_or_module) or path_or_module.endswith(".py"):
                file_path = os.path.abspath(path_or_module)
                module_name = f"dynamic_universe_{abs(hash(file_path))}"
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"Could not load Python script from '{file_path}'")
                mod = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = mod
                spec.loader.exec_module(mod)
                if not hasattr(mod, func_name):
                    raise AttributeError(f"Script '{file_path}' has no function or class '{func_name}'")
                fn = getattr(mod, func_name)
            else:
                mod = importlib.import_module(path_or_module)
                if not hasattr(mod, func_name):
                    raise AttributeError(f"Module '{path_or_module}' has no function or class '{func_name}'")
                fn = getattr(mod, func_name)
        else:
            raise TypeError(f"Invalid target for CodeUniverseProvider: {type(self.target)}")

        result = fn(**kwargs) if callable(fn) else fn
        if hasattr(result, "get_symbols") and callable(getattr(result, "get_symbols")):
            result = result.get_symbols(**kwargs)

        if not isinstance(result, (list, tuple, set)):
            raise ValueError(f"Code universe provider output must be a sequence of symbols, got {type(result)}")

        seen = set()
        out = []
        for s in result:
            s_clean = str(s).strip().upper()
            if s_clean and s_clean not in seen:
                seen.add(s_clean)
                out.append(s_clean)
        return out


_UNIVERSE_REGISTRY: Dict[str, Type[BaseUniverseProvider]] = {}


def register_universe_provider(name: str, provider_cls: Type[BaseUniverseProvider]):
    """Registers a universe provider class under a name."""
    _UNIVERSE_REGISTRY[name.lower()] = provider_cls


register_universe_provider("static", StaticUniverseProvider)
register_universe_provider("file", FileUniverseProvider)
register_universe_provider("code", CodeUniverseProvider)


def get_universe_provider(
    provider_name_or_instance: Union[str, BaseUniverseProvider, None] = None, **kwargs
) -> BaseUniverseProvider:
    """Factory to instantiate or retrieve a universe provider instance."""
    if isinstance(provider_name_or_instance, BaseUniverseProvider):
        return provider_name_or_instance

    if provider_name_or_instance is None:
        if "file_path" in kwargs:
            return FileUniverseProvider(kwargs["file_path"])
        elif "target" in kwargs:
            return CodeUniverseProvider(kwargs["target"])
        else:
            symbols = kwargs.get("symbols")
            return StaticUniverseProvider(symbols)

    name = str(provider_name_or_instance).lower()
    if name not in _UNIVERSE_REGISTRY:
        # Code-provider intent (an explicit "module_or_path:attr" specifier,
        # or a bare ".py" script relying on CodeUniverseProvider's
        # "get_universe" default attr) must be checked BEFORE the
        # file-existence check below -- otherwise a script path like
        # "my_script.py" that exists on disk would be swallowed as a
        # FileUniverseProvider and parsed as a plain-text symbol list
        # (silently returning garbage tokens from the source code) instead
        # of being executed as a script. _looks_like_code_specifier is
        # drive-letter-aware so a Windows absolute path like
        # "C:\\data\\universe.json" is NOT misdetected as a "path:attr" spec.
        if _looks_like_code_specifier(provider_name_or_instance):
            return CodeUniverseProvider(provider_name_or_instance)
        elif os.path.exists(provider_name_or_instance) or provider_name_or_instance.endswith(".json"):
            return FileUniverseProvider(provider_name_or_instance)
        else:
            raise ValueError(
                f"Unknown universe provider '{provider_name_or_instance}'. "
                f"Available providers: {list(_UNIVERSE_REGISTRY.keys())}"
            )

    provider_cls = _UNIVERSE_REGISTRY[name]
    return provider_cls(**kwargs)


def resolve_universe_symbols(
    symbols: Optional[Union[List[str], str]] = None,
    file_path: Optional[str] = None,
    provider: Union[str, BaseUniverseProvider, None] = None,
    provider_kwargs: Optional[dict] = None,
    default_symbols: Optional[List[str]] = None,
) -> List[str]:
    """Resolves a list of ticker symbols using the priority:
    1. Explicit file_path (if given)
    2. Explicit provider string / object (if given)
    3. Explicit symbols list (if given)
    4. Default symbols list (if given)
    """
    kwargs = provider_kwargs or {}
    if file_path:
        prov = FileUniverseProvider(file_path)
        return prov.get_symbols(**kwargs)
    elif provider:
        prov = get_universe_provider(provider, **kwargs)
        return prov.get_symbols(**kwargs)
    elif symbols:
        prov = StaticUniverseProvider(symbols)
        return prov.get_symbols(**kwargs)
    elif default_symbols:
        prov = StaticUniverseProvider(default_symbols)
        return prov.get_symbols(**kwargs)
    else:
        return []


def add_universe_cli_args(parser, default_universe: Optional[List[str]] = None):
    """Adds standardized universe selection CLI options to an argparse parser."""
    group = parser.add_argument_group("Universe Selection Options")
    default_help = f" (default: {' '.join(default_universe)})" if default_universe else ""
    group.add_argument(
        "--universe",
        "-u",
        nargs="+",
        default=None,
        help=f"List of ticker symbols to construct the universe{default_help}",
    )
    group.add_argument(
        "--universe-file",
        type=str,
        default=None,
        help="Path to a JSON or text file containing the symbol universe",
    )
    group.add_argument(
        "--universe-provider",
        type=str,
        default=None,
        help="Universe provider type ('static', 'file', 'code') or provider target string (e.g. 'script.py:get_universe')",
    )
    group.add_argument(
        "--universe-kwargs",
        type=str,
        default=None,
        help="JSON string or kwargs passed to custom code universe provider",
    )


def resolve_universe_from_args(args, default_symbols: Optional[List[str]] = None) -> List[str]:
    """Resolves ticker symbols from parsed command line arguments."""
    provider_kwargs = {}
    raw_kwargs = getattr(args, "universe_kwargs", None)
    if raw_kwargs:
        try:
            provider_kwargs = json.loads(raw_kwargs)
        except Exception as exc:
            raise ValueError(f"Failed to parse --universe-kwargs JSON string: {exc}")

    symbols = getattr(args, "universe", None)
    file_path = getattr(args, "universe_file", None)
    provider = getattr(args, "universe_provider", None)

    return resolve_universe_symbols(
        symbols=symbols,
        file_path=file_path,
        provider=provider,
        provider_kwargs=provider_kwargs,
        default_symbols=default_symbols,
    )
