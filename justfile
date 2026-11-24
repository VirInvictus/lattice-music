# lattice-music: PYTHONPATH=src is load-bearing (the site-packages copy is
# stale by design; the trap has bitten verification twice).

default:
    @just --list

test:
    PYTHONPATH=src python -m pytest tests/ -q

lint:
    ruff check src/ tests/

fmt:
    ruff format src/ tests/

version:
    @PYTHONPATH=src python -m lattice --version

# Regenerate docs/help.md from the live parser (pinned by tests/test_help_doc.py)
help-doc:
    PYTHONPATH=src python -m lattice --help > docs/help.md
