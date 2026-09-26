# Setup and upgrades

## Which winremote build this skill needs

The run queue, owned-job lifecycle, verified file transfers and `winremote-client`
were added to a maintained local fork of winremote-mcp. These changes have not
been published in the upstream release. Both the Windows MCP server and the
agent machine's client must include them. The implementation is shared across
projects; consuming projects supply their own commands and resource claims.

A plain upstream reinstall or package-manager upgrade can replace this build
and remove its capabilities. Upgrade from the maintained source: integrate
upstream changes there, validate the affected behavior, then build and deploy a
new wheel. Switching to upstream is a migration that requires verifying the
same capabilities and tests first.

## Discover the current installation

Read `~/.local/share/winremote/installation.json` on the agent machine. The
Windows copy is under `%USERPROFILE%\.local\share\winremote\installation.json`.
The receipts record the source checkout/commits, installed version, wheel and
SHA-256, maintenance script and evidence location. The recorded source checkout
may live on the authoring Mac; it is not necessarily a Windows filesystem path.
Resolve the installed skill's symlink if the receipt is missing: its canonical
source lives at `skill/winremote-host-runs` inside the maintained repository.

Use `winremote-client list` for a real MCP call and the server's Python path,
machine ID and active runs. Use `winremote-client --local list` for the agent
machine's separate queue. An existing upstream server without RunList can be
inspected with its actual GetRunningTasks tool before bootstrap installation.

The client reads the existing Codex/Claude winremote HTTP configuration; an
explicit `--config` file can select another connection. Preserve that endpoint,
authentication, server configuration and launch-task settings during upgrades.
Keep credentials in local configuration, outside deployment receipts.

## Install or update

1. Select the maintained source commit or an already verified wheel from the
   receipt. For changed code, run the scoped checks in the repository's
   `docs/host-runs.md`, including native Windows lifecycle checks when affected.
   Build from that checkout with `uv build --wheel --out-dir <release-dir>`.
   Record the wheel's SHA-256 and the exact source commit.
2. Install the client on the agent machine with
   `uv tool install --force <absolute-wheel-path>`. Verify
   `winremote-client --help`. Keep one canonical skill directory in the source
   repository and point global agent discovery links to it.
3. Stage the same wheel on Windows through native MCP transfer and verify its
   SHA-256 before installation. An upgraded server supports
   `winremote-client upload`. For an existing upstream server, use its real
   FileUpload tool through the official MCP SDK, reading and encoding the file
   programmatically so binary payloads never pass through model text. A new
   host with no working MCP connection needs local provisioning first.
4. Drain workloads before replacing the server package; coordinate unrelated
   work instead of cancelling it. In an existing server environment, preserve
   the validated dependencies with:

   ```text
   uv pip install --python <server-python.exe> --no-deps --reinstall <host-wheel>
   ```

   Use `--no-deps` only when that environment satisfies the selected build's
   dependencies. For a fresh Windows installation, run
   `uv tool install <host-wheel>` locally, then configure the real MCP server
   using the maintained repository's setup documentation. Do not assume this
   creates the host's launch task or client connection automatically.
5. On the configured Windows host, deploy the maintained
   `tools/restart-server-task.ps1` and follow the restart procedure in
   [operations](operations.md#installation-and-maintenance). Use a bounded
   RunSubmit request with enough time for shutdown, listener release and startup.
   If the old server lacks RunSubmit, arrange an independent local maintenance
   process before stopping it. If MCP is offline, report that and request local
   recovery; keep the actual winremote transport.
6. Verify the activated server and installed client, then update both installation
   receipts with the deployed artifact/hash, source and runtime commits,
   dependency versions, maintenance path and verification evidence. An in-place
   package replacement may leave the package manager's original upstream
   requirement unchanged; the receipt and maintained wheel remain authoritative
   for future upgrades.

Keep `~/.local/share/winremote/runs` through upgrades. It contains durable job
state and machine identity; deleting it loses recovery information and changes
the benchmark identity. Keep known working deployment artifacts for recovery.

## Verify activation

Require a successful RunList call, the expected machine ID and native tools
RunSubmit/Status/Cancel/Log, UploadBegin/Chunk/Finish and ArtifactInfo/Read.
Run one short owned command through the installed client and require terminal
success with `cleanup: complete`. Round-trip a small binary and compare hashes.
Verify `--local` with its separate machine ID when installing the local runner.

Changes to scheduling or supervision also need the repository's behavioral
checks for overlap, exclusive barriers, cancellation and worker failure. Changes
to server restart need the native `tests/test_server_restart.py` check before
live maintenance. Reuse unchanged accepted evidence; documentation edits do not
require restarting the server or rerunning workloads.
