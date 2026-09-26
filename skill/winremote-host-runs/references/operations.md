# Run and transfer operations

`winremote-client --help` is the CLI authority. It uses the official MCP client
and the existing Codex/Claude `winremote` HTTP connection configuration. An
explicit `--config` JSON file can select another configured host. Credentials
stay in local configuration; do not put them in requests, logs or notifications.
The native tools are `RunSubmit`, `RunList`, `RunStatus`, `RunCancel`, `RunLog`,
`UploadBegin/Chunk/Finish` and `ArtifactInfo/Read`.

## Installation

The installed client and Windows server must both include the durable-run
extension. `~/.local/share/winremote/installation.json`, when present, records
this machine's maintained source checkout, installed version and deployment
artifacts. The implementation belongs in winremote, not a consuming project.
When changing it, use that source checkout and its scoped tests. A missing
capability is an installation failure, not permission to improvise a launcher.

For Windows scheduled-task server maintenance, use the maintained
`tools/restart-server-task.ps1` from the implementation checkout through
RunSubmit. Supply the exact task name, port and expected server command
fragment, and verify no unrelated work will be interrupted. It verifies
process identity, waits for the old listener to close and requires a new
listener. After reconnecting, verify RunList and a real owned command;
a scheduler restart receipt alone does not prove activation.

## Submit one bounded command

Write a JSON request file, then run `winremote-client submit <file>`. Example:

```json
{
  "command": ["C:\\tools\\python.exe", "tools\\check.py", "--out", "C:\\evidence\\run-17"],
  "cwd": "C:\\work\\checkout-17",
  "owner": "project/session",
  "request_id": "check-17",
  "timeout": 120,
  "queue_timeout": 600,
  "isolation": "shared",
  "workspace_access": "exclusive",
  "resources": {"path:C:\\evidence\\run-17": "exclusive"},
  "hidden": true
}
```

Use the executable reported by `RunList`'s machine metadata only when it is the
intended runtime; application-specific interpreters and pinned engines still
come from the project. `command` is argv, without shell quoting. A shell command
uses an explicit noninteractive shell executable as argv. The server also
suppresses interactive Git credential prompts for owned runs.

Cwd receives a path claim automatically. Additional `resources` map names to
`shared` or `exclusive`. All claims are acquired atomically. A whole-machine
exclusive request conflicts with every workload regardless of named resources.
Ordinary direct MCP operations also honor these barriers and fail with
`host_busy` rather than waiting invisibly. Use the run queue for work that
should wait; metadata/status/cancellation remain available.

```sh
winremote-client status <run-id>
winremote-client wait <run-id> --seconds 60
winremote-client log <run-id> --offset 0 --limit 8192
winremote-client cancel <run-id> --owner project/session
```

A wait that expires returns exit 3 with the still-active run; it does not cancel
or resubmit it. A terminal failure returns nonzero. `RunLog` uses byte offsets;
full stdout/stderr and the final JSON receipt remain in `output_dir`.
`cleanup_uncertain` retains resource claims until the process state is resolved.
Owner labels provide attribution, not authentication or permission to cancel
another person's work. Never infer ownership from a process name or start time.

The worker survives client or MCP-server disconnection. On Windows a gated
child joins its Job Object before the command can start; worker death closes
the job. Recovery verifies absence before releasing claims. Local POSIX runs
use owned process groups. Deliberately escaping containment, external service
creation and unrelated user processes are outside a command's cleanup promise;
such work needs its own explicit lifecycle and resource declaration.

## Source and evidence

```sh
winremote-client upload ./source.bundle 'C:\staging\source.bundle'
winremote-client download 'C:\evidence\run-17\bundle.zip' ./evidence/run-17.zip
winremote-client stage ./repo --commit HEAD --base <known-host-commit> \
  --repository 'C:\cache\project.git' --checkout 'C:\work\checkout-17' \
  --owner project/session
```

`stage` creates a Git bundle locally, uploads it, then submits bundle verification,
fetch and a new detached worktree through the same queue. Omit `--base` for a
self-contained bundle; use it only when its prerequisite commit is already in
the host repository. The repository may be an existing host repository or a
new bare cache. A pre-existing destination checkout is refused. Inspect the
staging run's completion and source receipt before using the checkout.
Keep the returned run ID: rerunning `stage` creates a new bundle/submission,
and an existing destination is deliberately refused.

Upload/download stream chunks between files and MCP calls, resume partial
transfers and verify length plus SHA-256 before atomic publication. Repeating a
transfer to identical completed content is safe; different existing content is
refused. Use fresh artifact paths. On interruption repeat the same transfer;
on a hash failure retain the partial file and diagnose the changed/corrupt
input. Binary payloads never need to be copied or rewritten by the model.

## Local execution

`winremote-client --local submit/list/status/cancel/wait` uses the same contract
with this machine's queue and process supervision. It starts no remote server
and uses no substitute transport. Windows-specific acceptance remains on
Windows; local benchmark evidence remains a separate machine series.
