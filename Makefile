.PHONY: install install-plugin-runtime dev api web test test-backend test-web lint validate-plugin demo clean

PLUGIN_RUNTIME_DIR ?= $(HOME)/.local/share/brainhub/venv

install:
	python3 -m pip install '.[dev]' ./adapters
	python3 -m pip install --force-reinstall --no-deps . ./adapters
	cd apps/web && npm install

install-plugin-runtime:
	python3 -m venv "$(PLUGIN_RUNTIME_DIR)"
	"$(PLUGIN_RUNTIME_DIR)/bin/python" -m pip install . ./adapters
	"$(PLUGIN_RUNTIME_DIR)/bin/python" -m pip install --force-reinstall --no-deps . ./adapters
	"$(PLUGIN_RUNTIME_DIR)/bin/brainhub" --help >/dev/null

dev:
	@echo "Run 'make api' and 'make web' in separate terminals."

api:
	brainhub serve --host 127.0.0.1 --port 8420

web:
	cd apps/web && npm run dev

test: test-backend test-web validate-plugin

test-backend:
	python3 -m pytest packages/core/tests adapters/tests

test-web:
	cd apps/web && npm test && npm run build

lint:
	python3 -m ruff check packages/core adapters
	cd apps/web && npm run lint

validate-plugin:
	python3 scripts/verify_contracts.py

demo:
	BRAINHUB_DB_PATH=/tmp/brainhub-demo.db brainhub demo --reset

clean:
	@echo "Delete .venv, node_modules, and .brainhub manually if you intend to remove local state."
