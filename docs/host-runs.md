# Durable runs and artifacts

`winremote-client` is a file-oriented client of this server's MCP API. Use
`winremote-client --help` for commands. It discovers the existing Codex or
Claude `winremote` connection; `--config` selects an explicit `{url, headers}`
JSON connection. `--local` uses a separate machine-local queue.

RunSubmit accepts argv, an absolute cwd, owner, request_id, timeout,
queue_timeout, isolation (`shared` or `exclusive`), workspace_access and a
resource map (`shared` or `exclusive`). Cwd is exclusively claimed by default.
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
paths. RunCancel requests cancellation; inspect the eventual cleanup receipt.
A repeated owner/request_id is idempotent only with an identical specification.
Owner is attribution within the server's authenticated trust domain, not an
additional authorization boundary.

UploadBegin/Chunk/Finish and ArtifactInfo/Read provide resumable file transfer.
The CLI handles bytes; neither agent context nor manual encoding is involved.
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

For a server installed as a Windows scheduled task, use
`tools/restart-server-task.ps1` through RunSubmit with its exact task name,
port and expected server command fragment. First check that maintenance will
not interrupt unrelated work. Stopping a console-wrapper task alone can leave
the server alive; the script verifies the listener's process identity before
stopping it and requires a new listener afterward. Verify RunList and a real
owned command after reconnecting. Never infer successful activation from the
task's restart receipt alone.
