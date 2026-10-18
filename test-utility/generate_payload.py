#!/usr/bin/env python3
"""
Log-pipeline test utility (standard library only, Python 3.8+).

  python generate_payload.py print   --scenario mixed          # show the OTLP JSON
  python generate_payload.py send    --url http://localhost:32080 --api-key router-key
  python generate_payload.py suite   --url http://localhost:32080   # auth + routing demo, prints a table
  python generate_payload.py fluentd --url http://localhost:9880 --tag app.debug

Scenarios
  mixed   one of every severity, PII inside DEBUG logs   (use with the router key)
  debug   only DEBUG logs full of PII                    (use with the pii key)
  alerts  only WARN/ERROR/FATAL, incl. noisy health checks (use with the filter key)
"""
import argparse, json, os, sys, time, urllib.request, urllib.error

# ---- default demo keys: MUST match deploy/k8s/envoy.yaml -------------------
KEYS = {
    "router": os.getenv("ROUTER_KEY", "router-key-demo"),   # -> router -> pii / filter / drop
    "pii":    os.getenv("PII_KEY",    "pii-key-demo"),      # -> pii agent directly
    "filter": os.getenv("FILTER_KEY", "filter-key-demo"),   # -> filtering agent directly
}

SEV = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}


def kv(k, v):
    return {"key": k, "value": {"stringValue": str(v)}}


def rec(sev, body, attrs=None, number=True):
    return {
        "timeUnixNano": str(time.time_ns()),
        "severityNumber": SEV[sev] if number else 0,
        "severityText": sev,
        "body": {"stringValue": body},
        "attributes": [kv(k, v) for k, v in (attrs or {}).items()],
    }


def debug_logs():
    return [
        rec("DEBUG", "login attempt email=john.doe@example.com phone=+1 415-555-0132 ssn=123-45-6789",
            {"user_name": "John Doe", "address": "12 Palm Street, Dubai", "http.request.header.authorization": "Bearer abc"}),
        rec("DEBUG", "charging card 4111 1111 1111 1111 for order 8841, dob 1990-04-12",
            {"payment.contact": "billing@acme.com", "payment.card": "4111-1111-1111-1111"}),
        rec("TRACE", "cache miss for user jane@corp.io", {"user_name": "Jane Roe"}),
        rec("debug".upper(), "text-only severity from a legacy sender, call 050-123-4567", number=False),
    ]


def alert_logs():
    return [
        rec("WARN",  "disk usage 91% on node-3", {"host": "node-3"}),
        rec("ERROR", "payment gateway timeout order=8841", {"service": "checkout", "process.command_line": "java -jar app.jar --secret=x"}),
        rec("FATAL", "database primary unreachable", {"service": "orders"}),
        rec("ERROR", "health check failed for /healthz", {"http.target": "/healthz"}),   # dropped by filter
        rec("WARN",  "connection reset by peer while calling inventory"),              # dropped by filter
    ]


def info_logs():
    return [rec("INFO", "service started version=1.4.2")]                               # dropped by router


def build(scenario):
    logs = {"mixed": debug_logs() + alert_logs() + info_logs(),
            "debug": debug_logs(), "alerts": alert_logs()}[scenario]
    return {"resourceLogs": [{
        "resource": {"attributes": [kv("service.name", "demo-shop"), kv("deployment.environment", "demo")]},
        "scopeLogs": [{"scope": {"name": "test-utility"}, "logRecords": logs}],
    }]}


def post(url, data, headers):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode().strip()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode().strip()
    except Exception as e:                       # connection refused etc.
        return 0, str(e)


def otlp_send(base, prefix, key, scenario):
    h = {"Content-Type": "application/json"}
    if key:
        h["x-api-key"] = key
    return post(f"{base.rstrip('/')}{prefix}/v1/logs", build(scenario), h)


def cmd_print(a):
    print(json.dumps(build(a.scenario), indent=2))


def cmd_send(a):
    key = KEYS.get(a.api_key, a.api_key)
    for i in range(a.repeat):
        code, body = otlp_send(a.url, a.prefix, key, a.scenario)
        print(f"[{i+1}/{a.repeat}] POST {a.prefix}/v1/logs  scenario={a.scenario}  -> HTTP {code} {body}")
        if i + 1 < a.repeat:
            time.sleep(a.interval)


def cmd_suite(a):
    cases = [  # name, prefix, key, scenario, expected HTTP code
        ("no api key",                 "",        None,           "mixed",  401),
        ("wrong api key",              "",        "nope",         "mixed",  401),
        ("pii key on router path",     "",        KEYS["pii"],    "mixed",  401),   # keys are per destination
        ("router key  -> router",      "",        KEYS["router"], "mixed",  200),
        ("pii key     -> pii agent",   "/pii",    KEYS["pii"],    "debug",  200),
        ("filter key  -> filter agent", "/filter", KEYS["filter"], "alerts", 200),
        ("router key on /pii path",    "/pii",    KEYS["router"], "debug",  401),
    ]
    ok = True
    print(f"{'case':32} {'path':16} {'expect':>6} {'got':>5}")
    for name, prefix, key, sc, want in cases:
        code, _ = otlp_send(a.url, prefix, key, sc)
        flag = "OK " if code == want else "BAD"
        ok &= code == want
        print(f"{name:32} {prefix + '/v1/logs':16} {want:>6} {code:>5}  {flag}")
    print("\nNow check the sinks / agent logs - see README 'What to look for'.")
    sys.exit(0 if ok else 1)


def cmd_fluentd(a):
    """Fluentd in_http: POST /<tag> with a JSON record; tag drives routing inside fluentd."""
    for r in (debug_logs() if a.tag.endswith("debug") else alert_logs()):
        flat = {"severity": r["severityText"], "message": r["body"]["stringValue"]}
        flat.update({x["key"]: x["value"]["stringValue"] for x in r["attributes"]})
        code, body = post(f"{a.url.rstrip('/')}/{a.tag}", flat, {"Content-Type": "application/json"})
        print(f"POST /{a.tag} -> HTTP {code} {body}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    sp = s.add_parser("print");  sp.add_argument("--scenario", default="mixed", choices=["mixed", "debug", "alerts"]); sp.set_defaults(f=cmd_print)
    sp = s.add_parser("send")
    sp.add_argument("--url", default="http://localhost:32080")
    sp.add_argument("--api-key", default="router", help="router | pii | filter | <literal key>")
    sp.add_argument("--prefix", default="", help="'' (router), /pii or /filter (direct to an agent)")
    sp.add_argument("--scenario", default="mixed", choices=["mixed", "debug", "alerts"])
    sp.add_argument("--repeat", type=int, default=1); sp.add_argument("--interval", type=float, default=1.0)
    sp.set_defaults(f=cmd_send)
    sp = s.add_parser("suite"); sp.add_argument("--url", default="http://localhost:32080"); sp.set_defaults(f=cmd_suite)
    sp = s.add_parser("fluentd"); sp.add_argument("--url", default="http://localhost:9880"); sp.add_argument("--tag", default="app.debug"); sp.set_defaults(f=cmd_fluentd)
    a = p.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
