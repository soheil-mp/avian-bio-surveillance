# Database tooling Makefile.
#
# All targets are safe to run from any directory; they cd into the repo root.
# Designed to work on Windows (with `make` from Git for Windows / Chocolatey)
# and Linux/macOS without modification.

REPO_ROOT := $(realpath $(dir $(lastword $(MAKEFILE_LIST)))/..)
COMPOSE   := docker compose --env-file "$(REPO_ROOT)/.env" --env-file "$(REPO_ROOT)/config/runtime.env" -f "$(REPO_ROOT)/infrastructure/docker/docker-compose.yml"
PSQL      := $(COMPOSE) exec -T timescaledb psql -U $${POSTGRES_USER:-root} -d $${POSTGRES_DB:-avian_biosurveillance}
PYTHON    ?= python3

.PHONY: help bootstrap psql migrate-status erd dict lint test

help:
	@echo "make bootstrap       Re-run db/bootstrap/init_database.sh against the running container"
	@echo "make psql            Open an interactive psql shell as root"
	@echo "make migrate-status  List rows in schema_migrations_tbl (which migrations have been applied)"
	@echo "make erd             Regenerate db/erd/data_model.drawio from db/erd/generate_erd.py"
	@echo "make dict            Regenerate docs/reference/data_dictionary.md from SQL files"
	@echo "make lint            Run sqlfluff lint over db/ (requires `pip install sqlfluff`)"
	@echo "make test            Run db-related pytest tests (tests/db/)"

bootstrap:
	$(COMPOSE) restart timescaledb-init

psql:
	$(COMPOSE) exec timescaledb psql -U $${POSTGRES_USER:-root} -d $${POSTGRES_DB:-avian_biosurveillance}

migrate-status:
	$(PSQL) -c "SELECT version, applied_at, filename FROM schema_migrations_tbl ORDER BY version;"

erd:
	cd "$(REPO_ROOT)" && $(PYTHON) db/erd/generate_erd.py

dict:
	cd "$(REPO_ROOT)" && $(PYTHON) db/dict/generate_dict.py

lint:
	cd "$(REPO_ROOT)" && sqlfluff lint db/ --dialect postgres

test:
	cd "$(REPO_ROOT)" && pytest tests/db/ -q
