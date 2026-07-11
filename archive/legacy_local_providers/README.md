# Legacy Local Provider Stubs — Archived (Phase 7.6)

Archived during Phase 7 (model routing robustness) on 2026-06-21.

## What these are

Four per-family Ollama **provider stub files** that existed in
`models/local/{llama,mistral,qwen,deepseek}/` but were
**never registered in `ModelRouter._providers`** and therefore never
reachable by any inference path. Confirmed dead by grepping the entire
codebase for imports (zero hits outside themselves).

Archived:

| Directory   | File                     | Described role                              |
|-------------|--------------------------|---------------------------------------------|
| `llama/`    | `llama_provider.py`      | Compatibility provider, secondary local     |
| `mistral/`  | `mistral_provider.py`    | Lightweight assistant, fallback reasoning   |
| `deepseek/` | `deepseek_provider.py`   | Reflection, debugging, code review          |
| `qwen/`     | `qwen_provider.py`       | Primary Qwen chat (patched to qwen2.5:1.5b) |

## ⚠ CORRECTION — qwen_coder was NOT archived

`models/local/qwen_coder/` was **incorrectly included** in the initial
Phase 7.6 archive list. It is NOT a Python provider stub.

It is the **OpenVINO model file store** for `qwen2.5-coder:7b-instruct-int4`:
the directory where `openvino_model.xml`, `openvino_model.bin`, and the
tokenizer IR files live after export. `QwenOpenVINOProvider` discovers
this directory as its secondary model path candidate.

The archive copy of `qwen_coder/` here is empty and should be ignored.
The live directory at `models/local/qwen_coder/` was restored to its
correct state (a model file store with a README, no `__init__.py`).

See `models/local/qwen_coder/README.md` for export instructions.

## What replaced the archived stubs

The **single `OllamaProvider`** (`models/local/ollama/ollama_provider.py`)
registered as `"ollama"` in `ModelRouter._providers` handles all
locally-pulled Ollama models — including llama2, mistral, deepseek-r1,
qwen3, qwen2.5-coder, and every other tag pulled via `ollama pull`.

`ModelSwitcher.switch("ollama", "<tag>")` selects whichever specific tag
the user wants. No per-family stub is needed for Ollama-backed models.

The OpenVINO provider (`qwen_openvino`) remains a live registered provider
and is NOT affected by this archival.

## Available Ollama models on this machine (as of archival)

```
NAME                                     SIZE
huihui_ai/qwen2.5-coder-abliterate:7b   4.7 GB
huihui_ai/qwen2.5-coder-abliterate:3b   1.9 GB
qwen2.5-coder:7b-instruct-q4_K_M        4.7 GB
qwen3:4b                                 2.5 GB
qwen2.5:1.5b                             986 MB
qwen2.5-coder:7b                         4.7 GB
phi3:latest                              2.2 GB
llava:latest                             4.7 GB
deepseek-coder:latest                    776 MB
deepseek-r1:latest                       5.2 GB
qwen2.5-coder:1.5b                       986 MB
phi3:mini                                2.2 GB
mistral-openorca:7b-q4_K_M               4.4 GB
llama2:latest                            3.8 GB
mistral:7b                               4.4 GB
gemma:2b                                 1.7 GB
```
