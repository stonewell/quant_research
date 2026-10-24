"""Writer/reader agreement on the DEFAULT live-artifact directory (multi-strategy netting).

Why this suite exists
---------------------
`scripts/run_live_multi_strategy.py` WRITES a netted ticket
(`netted_trading_ticket.csv` / `.json`, `friction_savings_report.json`,
`sub_ledgers.json`) into its `--output-dir`; `web/livetrading/storage.py`'s
`StorageManager.get_multi_netting_run` READS a netted run back, and when the dated
archive (`<archive_dir>/<date>/multi_netted_<matrix>`) has no run yet it falls back to
that same default directory (a CLI-produced ticket is never copied into the archive).

Until 2026-10-23 both ends independently spelled
`<repo_root>/docs/ruleset/multi_strategy_netted`. That tree holds hand-authored
operating manuals and is being deleted, so:
  * the CLI default now comes from `scripts/live_paths.py` (an ordinary workspace
    artifact directory, `<repo_root>/scripts/results/live/multi_strategy_netted`);
  * the web fallback imports the same resolver instead of spelling a path, so it can
    neither point at a tree the CLI has stopped writing to (a silent-empty-read) nor
    drift from the writer.

100% offline: argparse only (no market data, no network, nothing spawned), and the
reader test writes a synthetic ticket into `tmp_path` -- never into the real
`scripts/results/live/` tree, `docs/`, or `data/`.
"""

import importlib.util
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import live_paths
from scripts import run_live_multi_strategy as multi

_DOCS_ROOT = os.path.join(_REPO_ROOT, "docs")


def _load_web_storage():
    """Loads `web/livetrading/storage.py` as a standalone script module.

    The web app imports its own modules by bare name (`import storage`) after putting
    `web/livetrading` on `sys.path`; loading it under a unique module name here keeps
    this suite independent of that path setup and of the web test package's own
    conftest (which reseats `LIVETRADING_*` directories).
    """
    path = os.path.join(_REPO_ROOT, "web", "livetrading", "storage.py")
    spec = importlib.util.spec_from_file_location("web_livetrading_storage_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. The writer: the netting CLI's default must not be inside `docs/`
# ---------------------------------------------------------------------------


def test_multi_strategy_default_output_dir_is_not_under_docs(monkeypatch):
    """Regression guard: `--output-dir` must default outside `docs/`, via the resolver.

    DISCRIMINATING: the pre-2026-10-23 literal was
    `<repo>/docs/ruleset/multi_strategy_netted`, so the `docs/` containment assertion
    (and the `ruleset` component check) fails on the old code rather than being a
    tautology.
    """
    out_dir = multi.DEFAULT_OUTPUT_DIR

    # Bound to the shared resolver, not to a second literal -- a re-spelled path in
    # either the writer or `scripts/live_paths.py` turns this red.
    assert out_dir == live_paths.default_live_output_dir(live_paths.MULTI_STRATEGY_KEY)
    assert os.path.basename(os.path.normpath(out_dir)) == live_paths.MULTI_STRATEGY_KEY

    # No netting run may recreate the hand-authored documentation tree.
    assert os.path.isabs(out_dir), out_dir
    # `os.path.relpath` (not `commonpath`) so a cross-drive Windows path cannot raise
    # here and masquerade as a failure of something else.
    assert os.path.relpath(os.path.abspath(out_dir), _DOCS_ROOT).startswith(".."), out_dir
    assert "ruleset" not in os.path.normpath(out_dir).split(os.sep), out_dir

    # The CLI's own argparse binding agrees with the constant, and merely PARSING the
    # command line (no `--output-dir`) creates nothing on disk.
    existed_before = os.path.exists(out_dir)
    monkeypatch.setattr(sys, "argv", ["run_live_multi_strategy.py", "--data-provider", "synthetic"])
    args = multi.parse_args()
    assert args.output_dir == out_dir
    assert os.path.exists(out_dir) == existed_before


# ---------------------------------------------------------------------------
# 2. The reader: the web fallback is the exact directory the CLI writes
# ---------------------------------------------------------------------------


def test_web_multi_netting_fallback_dir_is_the_cli_default():
    """The web reader must fall back to the CLI's default directory -- same resolver.

    Before 2026-10-23 `storage.py` spelled
    `<repo_root>/docs/ruleset/multi_strategy_netted` itself, independently of the
    writer. After the tree is deleted that literal would resolve to a directory the
    CLI no longer writes (a silent-empty-read); this pins both ends to one owner.
    """
    storage = _load_web_storage()

    assert storage.MULTI_NETTING_FALLBACK_DIR == multi.DEFAULT_OUTPUT_DIR
    assert storage.MULTI_NETTING_FALLBACK_DIR == live_paths.default_live_output_dir(
        live_paths.MULTI_STRATEGY_KEY
    )
    # ... and it is a workspace artifact directory, not the deleted docs tree.
    assert os.path.relpath(
        os.path.abspath(storage.MULTI_NETTING_FALLBACK_DIR), _DOCS_ROOT
    ).startswith(".."), storage.MULTI_NETTING_FALLBACK_DIR


def test_web_reader_finds_tickets_the_cli_writes_into_the_fallback_dir(tmp_path, monkeypatch):
    """Behavioral half of the contract: the fallback branch actually loads the run.

    A CLI-produced netted ticket never enters the dated archive, so the fallback is
    the ONLY path by which the web app can show it. The fallback directory is
    redirected into `tmp_path`; the real `scripts/results/live/` tree is never read or
    written.
    """
    storage = _load_web_storage()

    fallback_dir = tmp_path / "scripts" / "results" / "live" / live_paths.MULTI_STRATEGY_KEY
    fallback_dir.mkdir(parents=True)
    ticket = [
        {"symbol": "600000.SH", "action": "BUY", "delta_shares": 100, "price": 10.0},
        {"symbol": "BIL", "action": "HOLD", "delta_shares": 0, "price": 1.0},
    ]
    (fallback_dir / "netted_trading_ticket.json").write_text(
        json.dumps(ticket, ensure_ascii=False), encoding="utf-8"
    )
    (fallback_dir / "friction_savings_report.json").write_text(
        json.dumps({"total_friction_saved": 12.5}), encoding="utf-8"
    )
    (fallback_dir / "sub_ledgers.json").write_text(
        json.dumps({"chan_crisis_shield_blend": {"cash": 1.0}}), encoding="utf-8"
    )

    monkeypatch.setattr(storage, "MULTI_NETTING_FALLBACK_DIR", str(fallback_dir))

    manager = storage.StorageManager(
        repo_root=_REPO_ROOT,
        data_dir=str(tmp_path / "data"),
        archive_dir=str(tmp_path / "archive"),
        cache_dir=str(tmp_path / "cache"),
        settings_dir=str(tmp_path / "settings"),
    )

    # No dated archive run for this matrix -> the fallback is the only source.
    bundle = manager.get_multi_netting_run("2026-01-05", "tri_core_blend")

    assert bundle is not None
    assert os.path.realpath(bundle["path"]) == os.path.realpath(str(fallback_dir))
    assert bundle["trading_ticket"] == ticket
    assert bundle["friction_savings"] == {"total_friction_saved": 12.5}
    assert bundle["sub_ledgers"] == {"chan_crisis_shield_blend": {"cash": 1.0}}
    assert bundle["has_csv"] is False

    # Sanity: an empty fallback directory must yield "no run", never an empty bundle --
    # that is the silent-empty-read this change prevents.
    empty_fallback = tmp_path / "empty_fallback"
    empty_fallback.mkdir()
    monkeypatch.setattr(storage, "MULTI_NETTING_FALLBACK_DIR", str(empty_fallback))
    assert manager.get_multi_netting_run("2026-01-05", "tri_core_blend") is None
