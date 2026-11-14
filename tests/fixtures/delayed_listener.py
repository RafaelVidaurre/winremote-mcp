import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# Model a cold Python/server import longer than the former 25-second deadline.
if len(sys.argv) > 2 and (Path(sys.argv[2]) / "resident-id").exists():
    time.sleep(30)

s = socket.socket()
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen()
# A real owned worker tests that restarting the scheduled server preserves jobs.
if len(sys.argv) > 2:
    root = Path(sys.argv[2])
    src = str(Path(__file__).resolve().parents[2] / "src")
    sys.path.insert(0, src)
    os.environ["PYTHONPATH"] = src
    from winremote.runs import Runs

    runs = Runs(root)
    marker = root / "resident.started"
    release = root / "resident.release"
    resident = runs.submit(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; import time; Path({str(marker)!r}).touch()\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(.05)",
        ],
        str(root),
        "test/restart-resident",
        request_id="resident",
        timeout=90,
    )
    (root / "resident-id").write_text(resident["id"])
    deadline = time.monotonic() + 10
    while not marker.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("Resident did not start")
        time.sleep(0.05)
code = """import base64,json,socket,sys,time,psutil
v=json.loads(sys.stdin.readline()); s=socket.fromshare(base64.b64decode(v['socket']))
while psutil.pid_exists(v['parent']): time.sleep(.05)
time.sleep(1.5);s.close()
"""
p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE)
p.stdin.write(
    (json.dumps({"socket": base64.b64encode(s.share(p.pid)).decode(), "parent": os.getpid()}) + "\n").encode()
)
p.stdin.close()
while True:
    time.sleep(1)
