# JARVIS AI OS — Contributing Guide

Thank you for your interest in contributing! This document covers the development workflow, code standards, and pull request process.

---

## Development Setup

```bash
# Clone the repository
git clone https://github.com/Ashurra0427/JARVIS_AI_OS.git
cd JARVIS_AI_OS

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
make install-dev
```

---

## Project Structure

```
boot/          — Startup/bootstrap orchestration
kernel/        — EventBus, EventRouter, ServiceRegistry, DI Container
memory/        — Working, episodic, semantic, vector memory
models/        — LLM providers, routing, prompts, embeddings
perception/    — Speech (STT/TTS), vision, OCR, observation
cognition/     — Reasoning, planning, decision, reflection
actions/       — Browser, desktop, filesystem, terminal, API tools
agents/        — Specialized AI agents (research, engineering, etc.)
tools/         — Tool registry and implementations
interface/     — HUD, panels, widgets, workspaces, themes
config/        — YAML configuration files
```

---

## Code Standards

- **Python**: 3.11 or 3.12 only (3.13 drops `audioop` used by speech modules)
- **Style**: enforced by `ruff` (see `Makefile` targets)
- **Types**: type hints required for new code; `mypy` checks `config/`, `kernel/`, `memory/`
- **Imports**: isort-style grouping (stdlib → third-party → local)
- **Line length**: 100 characters (E501 ignored in ruff, but keep it reasonable)

Run before committing:
```bash
make lint
make format
make typecheck
make test-quick
```

---

## Architecture Rules

1. **EventBus is king**: subsystems communicate via `publish`/`subscribe`, not direct calls
2. **No direct memory access**: agents use `MemoryRouter`, never touch stores directly
3. **DI Container**: register singletons in `boot/dependency_container.py`
4. **Phase ordering**: respect bootstrap phases (0-9) in `boot/bootstrap.py`
5. **Security**: all actions flow through `actions/security/` — never bypass guards

---

## Commit Messages

Format: `<type>(<scope>): <description>`

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `perf`, `ci`

Examples:
```
feat(perception): add noise calibration to microphone pipeline
fix(memory): prevent duplicate episodic entries on rapid saves
docs(readme): document Phase 10 completion status
```

---

## Pull Requests

1. Fork and create a feature branch from `main`
2. Make your changes, following the code standards above
3. Add tests for new functionality
4. Ensure `make test-quick` passes
5. Open a PR with a clear description of what and why

PRs that fail CI or don't include tests will not be merged.

---

## Testing

```bash
make test           # Full suite (includes hardware tests)
make test-quick     # Skip slow/hardware tests
make test-cov       # With HTML coverage report
```

Hardware-dependent tests (audio, display, GPU) are marked with pytest markers:
- `requires_audio`
- `requires_display`
- `requires_gpu`
- `slow`

---

## Security

If you discover a security vulnerability, please email the maintainer directly rather than opening a public issue. See `SECURITY.md` for details.

---

## License

By contributing, you agree that your contributions will be licensed under the MIT License. See `LICENSE` for details.
