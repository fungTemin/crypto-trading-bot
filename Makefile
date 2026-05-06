.PHONY: help install test backtest download clean lint paper-micro

VENV := /Users/zhifeng.zhou/Documents/python/venv/bin/activate

help:
	@echo "Crypto Trading Bot - Commands"
	@echo ""
	@echo "  make install      Install dependencies"
	@echo "  make test         Run all tests"
	@echo "  make test-cov     Run tests with coverage"
	@echo "  make download     Download historical data"
	@echo "  make backtest     Run backtest"
	@echo "  make paper        Start paper trading"
	@echo "  make lint         Run mypy type checking"
	@echo "  make clean        Remove generated files"

install:
	. $(VENV) && pip install -e ".[dev]"

test:
	. $(VENV) && python -m pytest tests/ -v

test-cov:
	. $(VENV) && python -m pytest tests/ -v --cov=src --cov-report=term-missing

download:
	. $(VENV) && python scripts/download_data.py --symbol BTC/USDT --timeframe 5m --days 365

backtest:
	. $(VENV) && python scripts/run_backtest.py --symbol BTC/USDT --strategy grid --export

paper:
	. $(VENV) && TRADING_MODE=paper python -m src.main

paper-micro:
	. $(VENV) && TRADING_MODE=paper CONFIG_PATH=config/micro_30usd.yaml python -m src.main

lint:
	. $(VENV) && mypy src/ --ignore-missing-imports

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .mypy_cache *.egg-info
