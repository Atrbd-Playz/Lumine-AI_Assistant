import sys, urllib.request
sys.path.insert(0, r"G:\Project\Lumine-AI_Assistant")
from agent.tests.harness.test_server import LocalTestServer
s = LocalTestServer(); base = s.start()
print("base", base)
try:
    req = urllib.request.Request(base + "/api/redirect-chain?count=3")
    with urllib.request.urlopen(req, timeout=10) as r:
        print("status", r.status, "final", r.geturl())
        print(r.read()[:80])
finally:
    s.stop()
