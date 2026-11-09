import sys
import logging
import runpy

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    stream=sys.stderr,
)
logging.getLogger().setLevel(logging.DEBUG)

if __name__ == '__main__':
    runpy.run_path('backtester/run_backtest.py', run_name='__main__')
