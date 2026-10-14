#!/usr/bin/env bash
# Start the OpsPilot demo: knowledge base, API and operator console.
#   scripts/demo.sh                  replay mode (recorded incidents, no cluster)
#   scripts/demo.sh live <scenario>  live mode; posts the scenario's alert to the API
set -euo pipefail

MODE="${1:-replay}"
SCENARIO="${2:-}"
MODEL="${OPSPILOT_LLM_MODEL:-qwen3:4b}"
OLLAMA="${OPSPILOT_OLLAMA_BASE_URL:-http://localhost:11434}"
API_PORT="${OPSPILOT_API_PORT:-8000}"
UI_PORT=8501

say() { printf '\033[1m%s\033[0m\n' "$*"; }

say "1/4 Checking the local model ($MODEL via Ollama)"
if ! curl -sf "$OLLAMA/api/tags" >/dev/null; then
  echo "Ollama is not reachable at $OLLAMA. Start it (macOS: brew services start ollama)." >&2
  exit 1
fi
if ! curl -sf "$OLLAMA/api/tags" | grep -q "\"$MODEL\""; then
  echo "Model $MODEL is not pulled. Run: ollama pull $MODEL" >&2
  exit 1
fi

say "2/4 Starting Weaviate and indexing the knowledge base (first run downloads ~150 MB of models)"
make --no-print-directory infra-up
uv run opspilot kb ingest --store weaviate --chunker markdown_section --if-empty

say "3/4 Starting the API ($MODE mode)"
export OPSPILOT_API_MODE="$MODE"
export OPSPILOT_API_TOKEN="${OPSPILOT_API_TOKEN:-$(openssl rand -hex 16)}"
uv run opspilot-api >runs/api.log 2>&1 &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true' EXIT INT TERM
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$API_PORT/healthz" >/dev/null && break
  sleep 1
done
curl -sf "http://127.0.0.1:$API_PORT/healthz" >/dev/null || { echo "API did not start; see runs/api.log" >&2; exit 1; }

if [[ "$MODE" == "live" && -n "$SCENARIO" ]]; then
  say "    Sending the $SCENARIO alert to the webhook"
  uv run opspilot faults alert "$SCENARIO" |
    curl -sf -X POST "http://127.0.0.1:$API_PORT/alerts" \
      -H "Authorization: Bearer $OPSPILOT_API_TOKEN" -H "Content-Type: application/json" \
      --data-binary @- && echo
fi

say "4/4 Operator console"
cat <<EOF

  Console   http://localhost:$UI_PORT
  API       http://localhost:$API_PORT/docs   (bearer token for writes: $OPSPILOT_API_TOKEN)
  Mode      $MODE$( [[ "$MODE" == "replay" ]] && echo " (recorded incidents; nothing touches a cluster)")

  Ctrl-C stops the console and the API. \`make down-all\` stops Weaviate and the model.

EOF
uv run streamlit run src/opspilot/ui/app.py --server.port "$UI_PORT"
