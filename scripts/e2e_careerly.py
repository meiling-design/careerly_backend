"""Real-engine E2E check for the Careerly analysis chain (P2 verification).

Run: ./venv/Scripts/python.exe scripts/e2e_careerly.py
Uses temp DBs only — never touches app/data/*.db.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate all dbs BEFORE imports that create connections
import app.core.auth_store as astore
astore.DB_PATH = os.path.join(tempfile.mkdtemp(), "auth.db")
import app.careerly.router as crouter
from app.careerly.store import CareerlyStore

crouter._store = CareerlyStore(os.path.join(tempfile.mkdtemp(), "careerly.db"))

resume_text = open("test_resume.txt", encoding="utf-8").read()
JD = (
    "岗位职责：负责核心业务后端服务的设计与开发，使用 Python/Go 构建高并发 API，"
    "参与系统架构设计与性能优化。"
    "任职要求：3年以上后端开发经验，精通 Python，熟悉 MySQL、Redis，"
    "了解 Docker 与 Kubernetes，有大规模分布式系统经验者优先。"
)

t0 = time.time()
from app.careerly.engine import run_analysis

project, resume_profile = run_analysis(title="高级后端工程师", raw_jd=JD, resume_text=resume_text)
print(f"REAL RUN OK — {time.time() - t0:.1f}s")
print("project :", project.id, "|", project.name)
print("profile :", sorted(resume_profile.keys()) or "(empty — parser degraded)")
print("job     :", project.jobs[0].title, "| source =", project.jobs[0].source)
m = project.results[0].matches[0]
print("match   : score=%d rank=%d" % (m.score, m.rank))
print("strength:", m.strengths[:5])
print("gaps    :", m.gaps[:5])
if m.gapDetails:
    g = m.gapDetails[0]
    print("detail0 :", g.name, "→", g.level, "|", (g.actions or [""])[0][:60])
print("suggest :", (m.suggestion or "")[:80])

crouter._store.save_project(1, project)
back = crouter._store.get_project(1, project.id)
assert back and back.results[0].matches[0].score == m.score
print("ROUNDTRIP OK — persisted & reloaded identical")
