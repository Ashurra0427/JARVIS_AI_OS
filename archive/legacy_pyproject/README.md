# Archived: original empty pyproject.toml

`pyproject.toml` at the repo root was a 0-byte file (confirmed on two
separate audits). It was never populated with build-system, project
metadata, or dependency info — `requirements.txt` has always been the
actual dependency source of truth for this project.

**Update:** a real `pyproject.toml` was subsequently written at the repo
root (PEP 621 `[project]` metadata + `[project.optional-dependencies]` for
the legacy-ui/PyQt6 and Windows-only extras, kept in sync with
`requirements.txt`). This empty placeholder is kept here only as the
historical artifact from before that decision — it is not meant to be
restored to the root.

