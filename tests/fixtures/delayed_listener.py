import base64
import json
import os
import socket
import subprocess
import sys
import time

s = socket.socket()
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen()
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
