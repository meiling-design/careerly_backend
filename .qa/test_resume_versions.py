"""Ad-hoc check for the multi-version resume store.

Run:  python .qa/test_resume_versions.py
Not a pytest file - it drives one temp DB and prints a pass/fail per assertion.
"""
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.careerly.store import CareerlyStore

tmp = Path(tempfile.mkdtemp()) / "t.db"
store = CareerlyStore(db_path=tmp)
UID = 6

failures = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  <- {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


print("[1] legacy single-baseline user is backfilled")
store.save_resume(UID, "legacy resume text", {"skills": ["python"], "roles": ["PM"]})
versions = store.list_resume_versions(UID)
check("one version after backfill", len(versions) == 1, versions)
check("it is current", versions[0]["isCurrent"] is True, versions)
check("label is V1", versions[0]["label"] == "V1", versions)

print("[2] uploading appends instead of overwriting")
v2 = store.add_resume_version(UID, "second resume", {"skills": ["go"], "roles": ["Eng"]}, file_name="cv2.pdf")
versions = store.list_resume_versions(UID)
check("two versions now", len(versions) == 2, versions)
check("new one is current", versions[0]["id"] == v2["id"], versions)
check("ordered current-first then newest", [v["label"] for v in versions] == ["V2", "V1"], versions)
check("fileName recorded", versions[0]["fileName"] == "cv2.pdf", versions)
check("baseline is the new text", store.get_resume(UID)["text"] == "second resume", store.get_resume(UID))

print("[3] set-current rewrites the analysis baseline")
ok = store.set_current_resume_version(UID, versions[1]["id"])
versions = store.list_resume_versions(UID)
check("set_current returned True", ok is True)
check("V1 is now current", versions[0]["label"] == "V1", versions)
check("exactly one current", sum(v["isCurrent"] for v in versions) == 1, versions)
check("baseline text switched", store.get_resume(UID)["text"] == "legacy resume text", store.get_resume(UID))

print("[4] current version cannot be deleted")
check("delete current -> 'current'", store.delete_resume_version(UID, versions[0]["id"]) == "current")

print("[5] archived version deletes; surviving labels never change")
check("delete V2 -> ok", store.delete_resume_version(UID, versions[1]["id"]) == "ok")
versions = store.list_resume_versions(UID)
check("one version left", len(versions) == 1, versions)
check("V1 still labelled V1", versions[0]["label"] == "V1", versions)

v3 = store.add_resume_version(UID, "third resume", {"skills": ["rust"]}, file_name=None)
versions = store.list_resume_versions(UID)
# Documented behaviour: next seq = max(surviving)+1, so deleting the
# highest-numbered version and re-uploading reuses that number.
check("next label is V2 (max surviving + 1)", versions[0]["label"] == "V2", versions)
check("V1 label unchanged", versions[1]["label"] == "V1", versions)
check("file_name None for paste", versions[0]["fileName"] is None, versions)

print("[6] unknown ids")
check("set_current unknown -> False", store.set_current_resume_version(UID, "nope") is False)
check("delete unknown -> not_found", store.delete_resume_version(UID, "nope") == "not_found")

print("[7] save_resume refreshes current in place (no new version)")
import sqlite3

before_ids = [v["id"] for v in store.list_resume_versions(UID)]
store.save_resume(UID, "current re-parsed", {"skills": ["rust", "c"]})
versions = store.list_resume_versions(UID)
check("no new version row", [v["id"] for v in versions] == before_ids, versions)
check("baseline text updated", store.get_resume(UID)["text"] == "current re-parsed", store.get_resume(UID))

# Read the version row directly - _version_row deliberately does not expose text.
conn = sqlite3.connect(tmp)
row = conn.execute(
    "SELECT text, profile FROM careerly_resume_versions WHERE user_id = ? AND is_current = 1",
    (UID,),
).fetchone()
conn.close()
check("version row text follows the baseline", row[0] == "current re-parsed", row)
check("version row profile follows the baseline", '"c"' in (row[1] or ""), row)

print("[8] other users are isolated")
store.add_resume_version(99, "someone else", {"skills": ["x"]})
check("uid 6 unaffected", len(store.list_resume_versions(UID)) == 2)
check("uid 99 has one", len(store.list_resume_versions(99)) == 1)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL PASSED")
