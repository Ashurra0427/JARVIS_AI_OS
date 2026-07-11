# ============================================================
# JARVIS AI OS — Build & Release Makefile  (P-30)
# ============================================================
# Usage:
#   make install       Install all Python dependencies
#   make install-dev   Install dev/test extras
#   make run           Auto-detect mode and launch JARVIS
#   make console       Launch text-only console mode
#   make server        Launch FastAPI backend on :8000
#   make test          Run full test suite
#   make test-quick    Run tests, skip slow/hardware tests
#   make check         Environment validation only
#   make release       Build distributable ZIP
#   make clean         Remove generated artefacts
#   make help          Show this message
# ============================================================

PYTHON       ?= python3
PIP          ?= $(PYTHON) -m pip
PROJECT_NAME  = JARVIS_AI_OS
VERSION      ?= $(shell git describe --tags --always 2>/dev/null || echo "dev")
DIST_DIR      = dist
BUILD_DIR      = build
RELEASE_ZIP   = $(DIST_DIR)/$(PROJECT_NAME)_$(VERSION).zip

# Directories excluded from the release ZIP
EXCLUDE_DIRS  = \
	__pycache__ \
	.git \
	.github \
	.venv \
	venv \
	env \
	.env \
	*.egg-info \
	build \
	dist \
	logs \
	datastore \
	test_chroma \
	archive \
	*.pyc \
	*.pyo \
	.pytest_cache \
	.mypy_cache \
	.ruff_cache \
	node_modules \
	agro_flutter_app \
	models/kokoro \
	.cache \
	workspace \
	tmp

.PHONY: all install install-dev run console server test test-quick check \
        release clean help lint format typecheck

# ---------------------------------------------------------------------------
# Default
# ---------------------------------------------------------------------------

all: help

# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

install:
	@echo "── Installing dependencies ──"
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "── Installing Playwright browsers ──"
	$(PYTHON) -m playwright install chromium || true
	@echo "✓ Install complete"

install-dev: install
	@echo "── Installing dev/test extras ──"
	$(PIP) install pytest pytest-asyncio pytest-cov hypothesis ruff mypy httpx
	@echo "✓ Dev install complete"

# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

run:
	$(PYTHON) start.py

console:
	$(PYTHON) start.py --mode console

hud:
	$(PYTHON) start.py --mode hud

server:
	$(PYTHON) start.py --mode server

web:
	$(PYTHON) start.py --mode web

check:
	$(PYTHON) start.py --check

# ---------------------------------------------------------------------------
# Testing  (P-29)
# ---------------------------------------------------------------------------

test:
	@echo "── Running full test suite ──"
	$(PYTHON) -m pytest tests/ \
		-v \
		--tb=short \
		--asyncio-mode=auto \
		-x
	@echo "✓ Tests complete"

test-quick:
	@echo "── Running quick tests (skip slow/hardware) ──"
	$(PYTHON) -m pytest tests/ \
		-v \
		--tb=short \
		--asyncio-mode=auto \
		-m "not slow and not requires_audio and not requires_display and not requires_gpu" \
		-x

test-cov:
	@echo "── Running tests with coverage ──"
	$(PYTHON) -m pytest tests/ \
		--cov=. \
		--cov-report=term-missing \
		--cov-report=html:build/coverage \
		--asyncio-mode=auto \
		--ignore=tests/test_voice_pipeline.py \
		-v
	@echo "✓ Coverage report: build/coverage/index.html"

test-ci:
	@echo "── CI test run ──"
	CI=true $(PYTHON) -m pytest tests/ \
		-v \
		--tb=short \
		--asyncio-mode=auto \
		--ignore=tests/test_voice_pipeline.py \
		--ignore=tests/test_voice_pipeline_diagnostics.py \
		-q

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

lint:
	@echo "── Linting with ruff ──"
	$(PYTHON) -m ruff check . --select E,F,W,I --ignore E501

format:
	@echo "── Formatting with ruff ──"
	$(PYTHON) -m ruff format .

typecheck:
	@echo "── Type-checking with mypy ──"
	$(PYTHON) -m mypy config/ kernel/ memory/ --ignore-missing-imports --no-error-summary

# ---------------------------------------------------------------------------
# Release packaging  (P-30)
# ---------------------------------------------------------------------------

release: clean-dist
	@echo "── Building release $(VERSION) ──"
	@mkdir -p $(DIST_DIR)
	@echo "── Creating $(RELEASE_ZIP) ──"
	@zip -r $(RELEASE_ZIP) . \
		$(foreach d,$(EXCLUDE_DIRS),--exclude "*/$(d)/*" --exclude "$(d)/*") \
		--exclude "*/.DS_Store" \
		--exclude "*/.env" \
		--exclude "*/.env.*" \
		--exclude "*/secrets.*" \
		--exclude "*/*.key" \
		--exclude "*/*.pem" \
		--exclude "*/logs/*" \
		--exclude "*/datastore/*" \
		--exclude "*/test_chroma/*" \
		--exclude "*/archive/*" \
		--exclude "*/__pycache__/*" \
		--exclude "*.pyc" \
		--exclude "*.pyo" \
		--exclude "*/.pytest_cache/*" \
		--exclude "*/.mypy_cache/*" \
		--exclude "*/.ruff_cache/*" \
		--exclude "*/build/*" \
		--exclude "*/dist/*" \
		--exclude "*/node_modules/*" \
		--exclude "*/agro_flutter_app/*" \
		--exclude "*/models/kokoro/*" \
		--exclude "*/.cache/*" \
		--exclude "*/workspace/*" \
		--exclude "*/tmp/*" \
		--exclude "*.onnx" \
		--exclude "*.bin" \
		--exclude "*.whl" \
		--exclude "*.mp3" \
		--exclude "*.wav" \
		--exclude "*.png" \
		--exclude "*.jpg" \
		--exclude "*.jpeg" \
		--exclude "*.gif" \
		--exclude "*.zip" \
		--exclude "*.tar*" \
		--exclude "*.gz" \
		--exclude "*.7z" \
		--exclude "*.iso" \
		--exclude "*.sqlite" \
		--exclude "*.db" \
		--exclude "*.log"
	@echo "✓ Release: $(RELEASE_ZIP)"
	@ls -lh $(RELEASE_ZIP)

release-checksums: release
	@echo "── Generating checksums ──"
	sha256sum $(RELEASE_ZIP) > $(RELEASE_ZIP).sha256
	@cat $(RELEASE_ZIP).sha256
	@echo "✓ Checksum: $(RELEASE_ZIP).sha256"

# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

clean-dist:
	@rm -rf $(DIST_DIR)

clean-build:
	@rm -rf $(BUILD_DIR)
	@find . -type d -name "__pycache__" -not -path "./.git/*" -exec rm -rf {} + 2>/dev/null || true
	@find . -name "*.pyc" -delete 2>/dev/null || true
	@find . -name "*.pyo" -delete 2>/dev/null || true
	@rm -rf .pytest_cache .mypy_cache .ruff_cache

clean-logs:
	@rm -rf logs/*.log logs/audit/ 2>/dev/null || true
	@echo "✓ Logs cleared"

clean: clean-dist clean-build
	@echo "✓ Clean complete"

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

help:
	@echo ""
	@echo "  JARVIS AI OS — Make targets"
	@echo ""
	@echo "  Setup"
	@echo "    install        Install all Python dependencies"
	@echo "    install-dev    Install + pytest, ruff, mypy"
	@echo ""
	@echo "  Running"
	@echo "    run            Auto-detect and launch JARVIS"
	@echo "    console        Text-only console REPL"
	@echo "    hud            Qt HUD interface"
	@echo "    server         FastAPI backend on :8000"
	@echo "    web            Backend + open web HUD in browser"
	@echo "    check          Environment validation only"
	@echo ""
	@echo "  Testing"
	@echo "    test           Full test suite"
	@echo "    test-quick     Skip slow/hardware tests"
	@echo "    test-cov       Tests with HTML coverage report"
	@echo "    test-ci        CI-safe run (skip hardware, quiet)"
	@echo ""
	@echo "  Quality"
	@echo "    lint           Ruff linter"
	@echo "    format         Ruff formatter"
	@echo "    typecheck      Mypy type checker"
	@echo ""
	@echo "  Release"
	@echo "    release        Build dist/$(PROJECT_NAME)_VERSION.zip"
	@echo "    release-checksums  Build + sha256 checksums"
	@echo ""
	@echo "  Cleaning"
	@echo "    clean          Remove dist/ and __pycache__/"
	@echo "    clean-logs     Remove log files"
	@echo ""

