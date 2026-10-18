# Log pipeline demo: authorise → route → filter / redact → Elastic

One front door (Envoy) checks an API key, one router (OTel Collector) looks *inside* the
payload and sends each log record to the right agent, and each agent does exactly one job.

```
                          x-api-key                 Bearer token (injected by Envoy)
 clients ───────────────► ENVOY :8080 ─────────────────────────────────────────────┐
 (apps, test-utility)     401 if key is wrong                                       │
                                                                                    ▼
   router-key  /v1/logs ─────────────────────────────────────────────────►  OTEL ROUTER  (routing connector)
   pii-key     /pii/v1/logs     ──────────────────────────┐                 │ severity DEBUG/TRACE │ WARN/ERROR/FATAL │ other
   filter-key  /filter/v1/logs  ───────────────┐          │                 ▼                      ▼                  ▼
                                               │          │          PII AGENT              FILTER AGENT          dropped
                                               │          └────────►  mask email/SSN/       drop noise,           (INFO, logged
                                               └───────────────────►  card/phone/name…      trim fields           by debug exporter)
                                                                             │ ES api-key A         │ ES api-key B
                                                                             ▼                      ▼
                                                              otel-logs-debug-redacted        otel-logs-alerts      (Elasticsearch)
```

## Why each piece exists

| Layer | Job | Why here |
|---|---|---|
| **Envoy** | Authorise (`x-api-key`), pick destination by **path**, hide the key from backends | Envoy is cheap, fast, and sees headers/paths – but *cannot* read OTLP bodies |
| **Router** (`routing` connector) | Split by **severity** inside the payload | Only the collector can look at each log record |
| **PII agent** | Redact before anything is stored | Debug logs are the ones that leak personal data |
| **Filtering agent** | Keep signal, drop noise | Warn/error volume must stay searchable and cheap |

### API keys – one per destination, two layers

| Who | Key | Where it's checked |
|---|---|---|
| Client → router | `router-key-demo` | Envoy, path `/v1/logs` |
| Client → PII agent directly | `pii-key-demo` | Envoy, path `/pii/v1/logs` |
| Client → filter agent directly | `filter-key-demo` | Envoy, path `/filter/v1/logs` |
| Envoy → each collector | `*-token-demo` (Bearer) | `bearertokenauth` extension on that collector, so nothing can bypass Envoy |
| Router → agents | one Bearer token per agent | same extension |
| PII agent → Elastic | `ES_API_KEY_DEBUG` | Elasticsearch (limit it to `otel-logs-debug-redacted`) |
| Filter agent → Elastic | `ES_API_KEY_ALERTS` | Elasticsearch (limit it to `otel-logs-alerts`) |

A key is only valid for its own path: the PII key on `/v1/logs` is a 401 (try it in the suite).

## Layout

```
agents/
  otel-collector/router/config.yaml          severity routing + per-destination tokens
  otel-collector/pii-redaction/config.yaml   transform processor (OTTL) redaction → Elastic
  otel-collector/filtering/config.yaml       filter + attribute trim → Elastic
  fluentd/pii-redaction/fluent.conf          same PII job, Fluentd flavour
  fluentd/filtering/fluent.conf              same filtering job, Fluentd flavour
deploy/envoy/envoy.yaml                      auth + routing + access log + admin stats
deploy/k8s/                                  Deployments/Services, secrets.example.yaml
kustomization.yaml                           `kubectl apply -k .` builds ConfigMaps from agents/ + envoy
test-utility/
  generate_payload.py                        OTLP payload generator + auth/routing suite
  mock_elasticsearch.py                      fake Elastic that prints key + index + document
  run_local.sh                               whole pipeline on one machine, no Kubernetes
```

## Try it

### A. On your machine (fastest way to *see* it)
Needs `envoy`, `otelcol-contrib` **0.106+**, `python3` on PATH.
```bash
./test-utility/run_local.sh
```
It starts everything, runs the suite, then prints (1) auth results, (2) what each sink received,
(3) how the router split traffic, (4) how many records the filter dropped.

### B. On Kubernetes (namespace `application-demo`)
```bash
cp deploy/k8s/secrets.example.yaml deploy/k8s/secrets.yaml      # edit the ES api keys
kubectl apply -f deploy/k8s/secrets.yaml
kubectl apply -k .
python test-utility/generate_payload.py suite --url http://<node-ip>:32080
```
Create the two scoped Elastic keys (use the `encoded` value in the secret):
```bash
curl -k -u elastic:$ES_PASSWORD -X POST https://<es>:9200/_security/api_key -H 'Content-Type: application/json' -d '{
 "name":"pii-agent","role_descriptors":{"w":{"indices":[{"names":["otel-logs-debug-redacted"],"privileges":["create_doc","auto_configure"]}]}}}'
# repeat with name "filter-agent" and index "otel-logs-alerts"
```

## What to look for (the "aha" moments)

1. **Suite table** – 401s for missing/wrong/cross-used keys, 200 for the right key on the right path.
2. **PII sink** (`otel-logs-debug-redacted`): `john.doe@example.com` → `[REDACTED_EMAIL]`, SSN, card, phone, date,
   `user_name`/`address` masked, `authorization` attribute deleted, `pii.redacted=true` added.
   Live view: `kubectl logs deploy/otel-agent-pii` (debug exporter, detailed).
3. **Alerts sink** (`otel-logs-alerts`): only WARN/ERROR/FATAL. The `/healthz` and "connection reset by peer"
   records are gone, `process.command_line` removed, `pipeline.stage=filtering-agent` added.
4. **INFO** goes nowhere by design (router → `logs/unrouted` → debug exporter only).
5. A record with *only* `severityText="DEBUG"` (no number) is still routed to the PII agent.

## Observing the pipeline itself (self-observability)

Every collector exposes its own telemetry – no extra tooling needed:

| What | Where | Example |
|---|---|---|
| Metrics | `:8888/metrics` | `otelcol_receiver_accepted_log_records` (in), `otelcol_exporter_sent_log_records{exporter=…}` (out per destination), `otelcol_exporter_send_failed_log_records` (trouble), `otelcol_processor_filter_logs_filtered` (dropped) |
| Health | `:13133/` | readiness/liveness probes |
| Live pipeline view | `:55679/debug/pipelinez` | zPages: shows the graph router → pipelines → exporters |
| Envoy | admin `:9901/stats`, JSON access log on stdout | `kubectl port-forward deploy/envoy-ingress 9901` → `curl localhost:9901/stats \| grep ingress_http.downstream_rq_4xx` |
| Fluentd | `:24220/api/plugins.json`, `:24231/metrics` | `pii_agent_records_in_total`, `filter_agent_records_kept_total` |

Sanity equation on the router: `accepted = sent{otlphttp/pii} + sent{otlphttp/filter} + sent{debug/unrouted}`.
Alert on `send_failed`, on a growing queue, and on the PII agent's `accepted` ≠ its ES `sent`.

## Fluentd variants
`agents/fluentd/*/fluent.conf` do the same jobs (tags `app.debug` / `app.alert`, input via HTTP on 9880).
They need `fluent-plugin-elasticsearch` and `fluent-plugin-prometheus`; `in_http` has no key check, so keep it behind Envoy.
Send test records with `python test-utility/generate_payload.py fluentd --url http://localhost:9880 --tag app.debug`.
**These were not run here** (no Fluentd available); the redaction regexes were checked in Ruby. Treat them as a starting point.
Known quirk: masking `user_name`/`address` writes `null` when the key is absent.

## Notes and hardening before production
* **Versions:** the router needs collector **≥ 0.106** (log-context routing; 0.98 rejects it). All three use 0.116.0 here.
* **Keys in `envoy.yaml` are inline demo values.** Render them from a Secret (initContainer + envsubst) or use SDS/ext_authz, and rotate.
* Never commit Elastic passwords; use the scoped API keys above. TLS is `insecure_skip_verify` for the demo – mount your CA.
* Redaction is regex-based: tune patterns to your data (the DATE rule also masks ISO dates in bodies) and test with real samples.
* The filtering path intentionally does **not** redact. If errors can carry PII, copy `transform/redact` from the PII agent.
* One replica per agent here; scale agents horizontally, and add `sending_queue`/`retry_on_failure` tuning to the exporters.
