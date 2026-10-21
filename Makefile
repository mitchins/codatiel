test:
	python -m pytest tests -q

scan-local:
	python -m codatiel_i2.cli scan --help

# Local-only verification (no CI in this repository).
# Works without manual virtualenv activation: the script re-execs under a
# repository virtualenv when this interpreter lacks pytest. Override with:
#   make verify-evidence-contract PYTHON=/path/to/python
PYTHON ?= python3
verify-evidence-contract:
	$(PYTHON) eval/verify_evidence_contract.py
