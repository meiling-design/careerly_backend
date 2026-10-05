"""End-to-end check of the multi-version resume API against a live backend.

Run:  PYTHONPATH=. ./venv/Scripts/python.exe .qa/test_resume_api.py [phone]
Drives the real HTTP surface - no mocking. Prints PASS/FAIL per assertion.
"""
import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://localhost:8000/api/v1"
PHONE = sys.argv[1] if len(sys.argv) > 1 else "18993602085"

failures = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (f"   <- {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


def call(method, path, token=None, body=None, multipart_text=None):
    """Returns (status, data|error). Sends multipart when multipart_text is given."""
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if multipart_text is not None:
        boundary = "----qa-boundary"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="text"\r\n\r\n'
            f"{multipart_text}\r\n--{boundary}--\r\n"
        ).encode()
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.data = payload
    elif body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


def versions(token):
    _, d = call("GET", "/resume", token)
    return (d or {}).get("data", {}).get("versions", [])


def dump(vs):
    for v in vs:
        print(f"      {'*' if v['isCurrent'] else ' '} {v['label']:<4} "
              f"id={v['id'][:8]} file={v['fileName']} at={v['uploadedAt']}")
    if not vs:
        print("      (none)")


print("== login ==")
st, d = call("POST", "/auth/login", body={"phone": PHONE})
check("login ok", st == 200 and d.get("success"), d)
token = d["data"]["access_token"]

print()
print("== 1. GET /resume - legacy baseline must backfill into one version ==")
vs = versions(token)
dump(vs)
check("at least one version", len(vs) >= 1, vs)
check("exactly one current", sum(v["isCurrent"] for v in vs) == 1, vs)
check("current label is V<n>", vs[0]["label"].startswith("V"), vs[0])

before = len(vs)

print()
print("== 2. POST /resume (paste text) - must APPEND, not overwrite ==")
st, d = call("POST", "/resume", token,
             multipart_text="QA 多版本验证简历 · 第二份 · 仅用于接口测试")
check("post ok", st == 200 and d.get("success"), d)
new_v = d["data"]["version"]
check("response carries the new version", bool(new_v), d.get("data"))
vs = versions(token)
dump(vs)
check(f"version count grew {before} -> {before + 1}", len(vs) == before + 1, vs)
check("new version is current", vs[0]["id"] == new_v["id"], vs)
check("exactly one current", sum(v["isCurrent"] for v in vs) == 1, vs)
check("uploadedAt is the new one", vs[0]["uploadedAt"] == new_v["uploadedAt"], vs)

current_id = vs[0]["id"]
archived_id = [v["id"] for v in vs if not v["isCurrent"]][0]

print()
print("== 3. the analysis baseline follows the new current ==")
_, d = call("GET", "/resume", token)
check("baseline text is the new paste",
      "QA 多版本验证简历" in (d["data"]["text"] or ""), d["data"]["text"][:60])

print()
print("== 4. PUT set-current on the archived version ==")
st, d = call("PUT", f"/resume/versions/{archived_id}/current", token)
check("set-current ok", st == 200 and d.get("success"), d)
vs = versions(token)
dump(vs)
check("archived one is now current", vs[0]["id"] == archived_id, vs)
check("exactly one current", sum(v["isCurrent"] for v in vs) == 1, vs)

_, d = call("GET", "/resume", token)
check("analysis baseline switched back",
      "QA 多版本验证简历" not in (d["data"]["text"] or ""),
      (d["data"]["text"] or "")[:60])

print()
print("== 5. DELETE the now-archived version ==")
st, d = call("DELETE", f"/resume/versions/{current_id}", token)
check("delete ok", st == 200 and d.get("success"), d)
vs = versions(token)
dump(vs)
check(f"version count back to {before}", len(vs) == before, vs)
check("deleted id is gone", all(v["id"] != current_id for v in vs), vs)

print()
print("== 6. DELETE the CURRENT version - must be refused with 409 ==")
st, d = call("DELETE", f"/resume/versions/{archived_id}", token)
check("refused", st == 409, (st, d))
check("error code is VALIDATION_ERROR",
      (d or {}).get("error", {}).get("code") == "VALIDATION_ERROR", d)

print()
print("== 7. unknown ids ==")
st, d = call("DELETE", "/resume/versions/does-not-exist", token)
check("delete unknown -> 404", st == 404, (st, d))
st, d = call("PUT", "/resume/versions/does-not-exist/current", token)
check("set-current unknown -> 404", st == 404, (st, d))

print()
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL PASSED")
