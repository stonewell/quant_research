"""The web UI must advertise the NEW run window, not the retired pre-close one.

(2026-11-12 ruling) The legal window is "today's close + daily publication grace -> the
NEXT session's open" (China: ~15:30 -> next 09:30, crossing midnight). The retired
pre-close boxes ("13:55-14:20" inspection / "14:10-15:00" ticketing) must not appear
anywhere an operator reads, because they invite a run whose prices do not exist yet.

The 5m layer publishes later (BaoStock ~20:00) and is a SEPARATE axis: the hint says
"建议 15:30–20:00" rather than pushing the window start to 20:00, so a strategy that does
not need 5m is not delayed.

100% offline: this only reads the shipped static asset.
"""

import os
import re

_WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INDEX = os.path.join(_WEB_DIR, "static", "index.html")


def _index_html():
    with open(_INDEX, encoding="utf-8") as f:
        return f.read()


def test_index_html_advertises_the_post_close_window():
    html = _index_html()
    assert 'id="run-window-hint"' in html
    assert "建议运行时间：今日收盘后至次日开盘前（建议 15:30–20:00）" in html


def test_index_html_does_not_advertise_the_retired_windows():
    html = _index_html()
    for retired in ("13:55", "14:20", "14:10-15:00", "14:10 – 15:00"):
        assert retired not in html, f"retired run window {retired!r} still in the UI"


def test_hint_mentions_the_separate_5m_publication_hint():
    """The 20:00 bit is a 5m hint, never a window start (that would delay every run)."""
    html = _index_html()
    block = html.split('id="run-window-hint"', 1)[1].split("</span>", 1)[0]
    assert "5m" in block
    assert "20:00" in block
    # The declared window itself still starts at the daily-bar publication time.
    assert "收盘后至次日开盘前" in block


def test_no_stale_web_copy_of_the_old_window_in_the_service_hint():
    service = os.path.join(_WEB_DIR, "execution_service.py")
    with open(service, encoding="utf-8") as f:
        source = f.read()
    assert "A 股 14:10 之后" not in source
    assert re.search(r"15:30", source) is not None
