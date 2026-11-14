# Durable runs and artifacts

`winremote-client` is a file-oriented client of this server's MCP API. Use
`winremote-client --help` for commands. It discovers the existing Codex or
Claude `winremote` connection; `--config` selects an explicit `{url, headers}`
JSON connection. `--local` uses a separate machine-local queue.

RunSubmit accepts argv, an absolute cwd, owner, request_id, timeout,
queue_timeout, isolation (`shared` or `exclusive`), workspace_access and a
resource map (`shared` or `exclusive`). Cwd is shared by default; declare mutated
paths explicitly or request exclusive workspace access.
Named resources must describe the same real object across clients: paths use
`path:<absolute path>`, ports use `port:tcp:<number>`, desktop interaction uses
`desktop`. Parent path claims conflict with descendants when either writes.

Admission is transactional. Compatible jobs overlap; older conflicting jobs
have priority without blocking disjoint jobs. Whole-machine exclusive jobs
form a barrier. They wait for existing jobs to drain, and later jobs wait for
their completion. Use this for benchmarks and any operation that needs complete
machine isolation. Control-plane status/cancellation remain usable; ordinary
MCP operations respect the barrier.

The SQLite queue and per-run evidence are in `~/.local/share/winremote/runs`.
An isolated `WINREMOTE_RUNS_DIR` is for tests; production clients must share the
default machine queue. A detached worker owns each run independently of client
or server lifetime. Windows uses a kill-on-close Job Object with a gated child;
POSIX uses a process group whose leader is retained until cleanup. The result
records actual cleanup. Uncertain cleanup retains claims. External services or
processes that deliberately escape containment require their own lifecycle.

RunStatus/RunList preserve machine identity, command, ownership and evidence
paths. Active listings include every active run; `limit` bounds terminal history only.
Routine reads and blocked admission polls do not acquire the SQLite writer lock.
Recovery checks process identity outside transactions and only updates an unchanged
snapshot; a busy writer leaves its claims conservatively retained. RunCancel requests cancellation; inspect the eventual cleanup receipt.
A repeated owner/request_id is idempotent only with an identical specification.
Owner is attribution within the server's authenticated trust domain, not an
additional authorization boundary.

RunSubmit returns a one-time observer token for a new run; only its SHA-256 is
stored, so RunStatus, RunList and idempotent retries never return it. While that
run is running or cleanup_uncertain, the token admits read-only observation
nested in the run's own claims:

- RunSnapshot gives a screenshot and window list, and requires the run's desktop
  claim.
- RunLog with `path` gives a growing-file byte tail inside the run's path claims
  or output directory.

The owning run and queued requests do not block these leases; every other held
conflicting run still yields host_busy. Out-of-claim, wrong-token and released-run
requests return observation_denied. Receipts go to the run's
`observations.jsonl`. Ordinary tools and their host_busy behaviour are unchanged.

UploadBegin/Chunk/Finish and ArtifactInfo/Read provide resumable file transfer.
The CLI handles bytes; neither agent context nor manual encoding is involved.
Downloads use 256 KiB chunks: legacy MCP duplicates base64 in text and structured
fields, so 512 KiB binary chunks exceeded newer clients' 1 MiB SSE event cap.
Uploads retain 512 KiB chunks because their replies contain only a small receipt.
Existing partial downloads resume and retain full SHA-256/length verification.

For a server older than hostruns.7.1, a verified temporary client workaround is:

```sh
uv run --with 'fastmcp==3.2.4' --with 'mcp==1.28.1' \
  --with 'winremote-mcp @ git+https://github.com/RafaelVidaurre/winremote-mcp.git@v0.4.23-hostruns.6' \
  winremote-client download '<remote-path>' '<local-path>'
```

This isolates dependency versions without changing global installs. Updating
only the client package does not shrink an older server's fixed chunks.
`FileDownload` sends the entire file and is not a bounded-transfer alternative.

Publication follows full length/hash verification and refuses an existing
file with different content. Git staging uses these transfers and queues
verification plus a new detached checkout; existing checkouts are never reset.

Benchmarks must name the machine ID and matched source, workload, runtime,
renderer/resolution and host conditions. Results from different machines form
separate series. Queue isolation does not suppress unrelated user programs or
OS work; the benchmark still owns its host-load validity checks.

Validation: `python -m pytest tests/test_host_runs.py tests/test_taskmanager.py
 tests/test_shell_tools.py tests/test_config_and_tiers.py tests/test_integration.py`.
Run on Windows for native Job Object, worker-crash and PowerShell evidence.

For a server installed as a Windows scheduled task, invoke
`tools/restart-server-task.ps1` through RunSubmit with its exact task name,
port, expected server command fragment, staged candidate `-Python`, and `-RunRoot`.
Pause submissions and let legacy workers finish, or have their owners save and
stop them. The preflight refuses those workers before touching the service;
upgrading a server does not update code already loaded by resident workers.
Console wrappers can leave their server alive, and process termination can precede socket release. The script verifies the original
listener's identity, waits for its socket to close, refuses a different owner,
and allows up to 120 seconds for a new listener (configurable). Verify RunList
and an owned command after reconnecting. The Windows regression test in `tests/test_server_restart.py`
uses a disposable task that continuously accesses the same SQLite queue, whose
listening socket outlives the original process. It verifies that a protected
worker survives a delayed restart and finishes normally. Protected workers retain
their workloads through metadata read errors while enforcing the original deadline;
cancellation resumes when metadata reads recover. Diagnostic error codes are in
`worker.log`.
For a live upgrade, install the new runtime side by side and keep the previous
runtime until its resident workers exit; replacing files beneath running workers
is not covered by the restart guarantee.
