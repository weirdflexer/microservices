PYTHON ?= python3
COMPOSE ?= docker compose

.PHONY: install test up down logs specs demo
install:
	$(PYTHON) -m pip install -r requirements-dev.txt
test:
	$(PYTHON) -m pytest tests/unit tests/component --cov --cov-report=term-missing
up:
	$(COMPOSE) up -d --build
down:
	$(COMPOSE) down
logs:
	$(COMPOSE) logs -f
specs:
	$(PYTHON) -m scripts.export_openapi
demo:
	$(PYTHON) -m scripts.demo
