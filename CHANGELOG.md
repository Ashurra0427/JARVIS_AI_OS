# Changelog

All notable changes to JARVIS AI OS will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial public release preparation
- GitHub Actions CI pipeline
- Dockerfile and docker-compose configuration
- Community documentation (CONTRIBUTING, SECURITY, CODE_OF_CONDUCT)
- .gitignore updates for model artifacts and caches
- MIT license alignment in pyproject.toml

### Changed
- Fixed hardcoded user path in Makefile release target

### Fixed
- License mismatch between pyproject.toml (Proprietary) and LICENSE (MIT)

## [0.2.0] — 2026-07-11

### Added
- Agro integration with Flutter mobile app
- Qwen-Coder local model provider (OpenVINO)
- Ollama local model provider
- DeepFilterNet noise suppression
- Proactive intelligence engine
- Knowledge feed ingestion pipeline
- Daily summary generation
- Browser workspace with ChromaDB integration
- Mobile HUD (web-based)
- Desktop Qt HUD (PySide6)
- WebSocket-based HUD server
- Vector memory with ChromaDB backend
- Episodic and semantic memory stores
- Agent RAG recall fixes
- Phase 12 server handlers and hardware-aware STT

### Changed
- Migration from PyQt6 to PySide6 for HUD
- Consolidated entry points (server.py, main.py, start.py)
- Refactored event bus to use async publish/subscribe
- Improved model router with provider fallback chains

### Fixed
- Voice pipeline diagnostics and hotword detection
- Memory store retrieval edge cases
- Chat panel responsive layout issues
- Embedding service token limits

## [0.1.0] — 2026-06-01

### Added
- Initial project structure
- Event-driven kernel (EventBus, EventRouter, ServiceRegistry)
- Dependency injection container
- Model router with Gemini, Groq, DeepSeek, Qwen, Llama, Mistral providers
- Perception pipeline (STT, TTS, vision, OCR)
- Cognition layer (reasoning, planning, decision, reflection)
- Action executors (browser, desktop, filesystem, terminal, API)
- Agent system (coordinator + specialists)
- Tool registry and implementations
- Memory system (working, episodic, semantic, vector)
- Qt HUD interface
- Web HUD interface
- FastAPI server with WebSocket support

[Unreleased]: https://github.com/Ashurra0427/JARVIS_AI_OS/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Ashurra0427/JARVIS_AI_OS/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Ashurra0427/JARVIS_AI_OS/releases/tag/v0.1.0
