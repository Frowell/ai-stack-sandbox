#!/usr/bin/env sh
# ILLUSTRATIVE — spec for gateway/entrypoint.sh.
#
# Cold-start warmup (README fail-mode / AC4). Presidio + the spaCy NER model load
# lazily on first use (seconds). Under the fail-closed default that latency would
# exceed the 300 ms per-call budget and BLOCK the first real chat/embedding call
# after every gateway start. So we pay the load ONCE here, before litellm binds the
# port — a passing healthcheck therefore implies a warm analyzer. The 300 ms budget
# measures steady-state per-call latency and explicitly excludes this one-time load.
set -e

echo "[entrypoint] warming Presidio + spaCy (one-time, excluded from the 300ms budget)..."
python - <<'PY'
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
a = AnalyzerEngine()          # loads the spaCy model installed at image build
an = AnonymizerEngine()
res = a.analyze(text="warmup jane@acme.com", entities=["EMAIL_ADDRESS"], language="en")
an.anonymize(text="warmup jane@acme.com", analyzer_results=res)  # throwaway pass
print("[entrypoint] warmup complete")
PY

# Only now start the server — readiness implies the analyzer is warm.
exec litellm --config /app/config.yaml --port 4000
