.PHONY: format lint typecheck test quality format-check build run portable

format:
	uv run --locked ruff format .

lint:
	uv run --locked ruff check .

typecheck:
	uv run --locked pyright

test:
	uv run --locked pytest --cov

quality: format-check lint typecheck test build

format-check:
	uv run --locked ruff format --check .

build:
	uv build --no-sources

run:
	uv run --locked lbr-dashboard

portable:
	uv sync --locked --extra dev --extra package
	uv run --locked python scripts/build_portable.py
	uv run --locked python scripts/smoke_portable.py dist/portable
