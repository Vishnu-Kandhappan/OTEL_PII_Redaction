#!/usr/bin/env bash
# Run the WHOLE pipeline on one machine (no Kubernetes): envoy + router + 2 agents + fake Elasticsearch,
# fire the test suite, show what each sink received and how the router split the traffic.
# Needs on PATH: envoy (1.30+), otelcol-contrib (0.106+), python3.     Usage: ./run_local.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; W="$(mktemp -d)"; A="$ROOT/agents/otel-collector"
echo "collector: $(otelcol-contrib --version)  (router needs >= 0.106)"
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT

# local copies: swap k8s service names for localhost ports, give each process its own ports
sed -e 's#http://otel-agent-pii:4318#http://127.0.0.1:4319#' -e 's#http://otel-agent-filter:4318#http://127.0.0.1:4320#' "$A/router/config.yaml" > "$W/router.yaml"
sed -e 's/0.0.0.0:4318/0.0.0.0:4319/' -e 's/0.0.0.0:8888/0.0.0.0:8889/' -e 's/0.0.0.0:13133/0.0.0.0:13134/' -e 's/0.0.0.0:55679/0.0.0.0:55680/' "$A/pii-redaction/config.yaml" > "$W/pii.yaml"
sed -e 's/0.0.0.0:4318/0.0.0.0:4320/' -e 's/0.0.0.0:8888/0.0.0.0:8890/' -e 's/0.0.0.0:13133/0.0.0.0:13135/' -e 's/0.0.0.0:55679/0.0.0.0:55681/' "$A/filtering/config.yaml" > "$W/filter.yaml"
sed -e 's/otel-router.application-demo.svc.cluster.local, port_value: 4318/127.0.0.1, port_value: 4318/' \
    -e 's/otel-agent-pii.application-demo.svc.cluster.local, port_value: 4318/127.0.0.1, port_value: 4319/' \
    -e 's/otel-agent-filter.application-demo.svc.cluster.local, port_value: 4318/127.0.0.1, port_value: 4320/' \
    -e 's/grafana.application-demo.svc.cluster.local, port_value: 80/127.0.0.1, port_value: 3000/' "$ROOT/deploy/envoy/envoy.yaml" > "$W/envoy.yaml"

export ROUTER_TOKEN=router-token-demo PII_AGENT_TOKEN=pii-agent-token-demo FILTER_AGENT_TOKEN=filter-agent-token-demo
export ES_ENDPOINT=http://127.0.0.1:9200 ES_API_KEY_DEBUG=KEY-DEBUG-SINK ES_API_KEY_ALERTS=KEY-ALERTS-SINK

python3 "$ROOT/test-utility/mock_elasticsearch.py" 9200 >"$W/es.log" 2>&1 &
otelcol-contrib --config "$W/pii.yaml"    >"$W/pii.log"    2>&1 &
otelcol-contrib --config "$W/filter.yaml" >"$W/filter.log" 2>&1 &
otelcol-contrib --config "$W/router.yaml" >"$W/router.log" 2>&1 &
envoy -c "$W/envoy.yaml" --base-id 42 >"$W/envoy.log" 2>&1 &
sleep 8

echo "=== 1. auth + routing suite (through Envoy :8080) ==="
python3 "$ROOT/test-utility/generate_payload.py" suite --url http://localhost:8080 || true
sleep 4
echo; echo "=== 2. what each Elasticsearch sink received (note the api key + index per sink) ==="
sed -E 's/"@timestamp": ?"[^"]*",? ?//; s/"(Resource|Scope|TraceFlags)": ?(\{[^}]*\}|"[^"]*"|[0-9]+),? ?//g' "$W/es.log" | cut -c1-330
echo; echo "=== 3. router self-metrics: how traffic was split ==="
curl -s localhost:8888/metrics | grep -E '^otelcol_(receiver_accepted_log_records|exporter_sent_log_records)' | sed -E 's/service_instance_id="[^"]*",|service_name="[^"]*",|service_version="[^"]*"//g' || true
echo; echo "=== 4. filtering agent: records dropped by filter/drop ==="
curl -s localhost:8890/metrics | grep -E '^otelcol_processor_filter_logs_filtered' | sed -E 's/service_instance_id="[^"]*",|service_name="[^"]*",|service_version="[^"]*"//g' || true
echo; echo "logs kept in $W"
