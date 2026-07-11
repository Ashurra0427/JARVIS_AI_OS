# models/local/qwen_coder/ — OpenVINO Model Files Directory

This directory holds the **OpenVINO IR export** of `qwen2.5-coder:7b-instruct-int4`
used by `models/local/qwen_openvino/qwen_openvino_provider.py`.

**This is NOT a Python package.** It has no `__init__.py`.
It is a model file store, not provider code.

## Expected files (place here after export)

```
openvino_model.xml
openvino_model.bin
openvino_tokenizer.xml
openvino_tokenizer.bin
openvino_detokenizer.xml
openvino_detokenizer.bin
tokenizer_config.json
config.json
generation_config.json
```

## How to export qwen2.5-coder:7b-instruct-int4 to OpenVINO IR

```bash
pip install optimum[openvino] openvino openvino-tokenizers

optimum-cli export openvino \
    --model Qwen/Qwen2.5-Coder-7B-Instruct \
    --weight-format int4 \
    --task text-generation-with-past \
    models/local/qwen_coder/
```

After export, the provider auto-discovers these files via
`QwenOpenVINOProvider._pick_default_model_dir()` which checks
`models/local/qwen_coder/` as its secondary candidate path.

## Primary lookup path

`QwenOpenVINOProvider` checks these paths in order:
1. `models/qwen_openvino/`   ← preferred (Phase 7.4 convention)
2. `models/local/qwen_coder/` ← legacy path (this directory)

First directory containing `openvino_model.xml` + `openvino_model.bin` wins.

## Why this directory was not archived in Phase 7.6

Phase 7.6 archived **Python provider stub files** that duplicated
`OllamaProvider`'s role. This directory is a **model file store** —
a different thing entirely. The archive README has been corrected.
