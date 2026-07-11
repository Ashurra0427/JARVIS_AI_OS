import os, zipfile

ROOT = r"C:\Users\bisha\Desktop\JARVIS_AI_OS"
OUT = r"C:\Users\bisha\Desktop\JARVIS_AI_OS_20260709.zip"

EXCLUDE_DIRS = {
    ".venv", "__pycache__", ".cache", ".pytest_cache", ".vscode",
    "logs", "node_modules", "agro_flutter_app", "build", "dist",
}
EXCLUDE_TOP = {
    ".venv", "logs", ".vscode", ".cache", ".pytest_cache",
    "agro_flutter_app",
}
# BUGFIX: "models" used to be in EXCLUDE_DIRS/EXCLUDE_TOP, on the assumption
# it only ever held downloaded model-weight binaries. It's ALSO the real
# top-level source package `models/` (models/router/model_router.py + the
# ollama/groq/gemini/qwen provider implementations — listed as "models*" in
# pyproject.toml right alongside memory*/kernel*). That blanket exclusion
# was silently deleting the entire LLM routing layer from every zip, which
# is why agents downstream (model_router=None) looked "dumb"/stubbed out.
# Exclude actual large weight-file formats by suffix instead, regardless of
# which directory they live in, so source code always ships.
EXCLUDE_SUFFIXES = (
    ".pyc", ".pyo",
    ".bin", ".gguf", ".ggml", ".onnx", ".safetensors",
    ".pt", ".pth", ".ckpt", ".h5", ".msgpack",
)
EXCLUDE_DIR_PARTS = ("browser_profile", "browser_profile_test")

def excluded(path_parts, rel):
    # top-level exclusions
    if path_parts[0] in EXCLUDE_TOP:
        return True
    for p in path_parts:
        if p in EXCLUDE_DIRS:
            return True
        if any(part in p for part in EXCLUDE_DIR_PARTS):
            return True
    if rel == os.path.basename(OUT):
        return True
    if rel.endswith(EXCLUDE_SUFFIXES):
        return True
    return False

count = 0
total = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # prune heavy dirs in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDE_DIRS
            and not any(part in d for part in EXCLUDE_DIR_PARTS)
        ]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT)
            parts = rel.split(os.sep)
            if excluded(parts, rel):
                continue
            if fn == os.path.basename(OUT):
                continue
            z.write(full, rel)
            count += 1
            total += os.path.getsize(full)

print(f"Zipped {count} files, ~{total/1024/1024:.1f} MB -> {OUT}")
