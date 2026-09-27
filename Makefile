.PHONY: help install run test deploy clean

help:
	@echo "Available commands:"
	@echo "  make install    - Install dependencies with uv"
	@echo "  make run        - Run local development server (port 8080)"
	@echo "  make test       - Run unit tests with pytest"
	@echo "  make deploy     - Deploy agent to Google Cloud Run"
	@echo "  make clean      - Clean cache and temporary files"

install:
	uv sync

run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

test:
	uv run pytest tests/unit -v

deploy:
	./deploy.sh

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
