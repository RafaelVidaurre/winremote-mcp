# Invoke through RunSubmit. Verify no unrelated work before server maintenance.
param(
    [Parameter(Mandatory=$true)][string]$TaskName,
    [Parameter(Mandatory=$true)][int]$Port,
    [Parameter(Mandatory=$true)][string]$ExpectedCommandFragment,
    [ValidateRange(1,600)][int]$StartupTimeoutSeconds = 120
)
$ErrorActionPreference = 'Stop'
$listener = @(Get-NetTCPConnection -LocalPort $Port -State Listen)
$ids = @($listener.OwningProcess | Select-Object -Unique)
if ($ids.Count -ne 1) { throw 'Expected exactly one server listener' }
$server = Get-CimInstance Win32_Process -Filter "ProcessId = $($ids[0])"
if (-not $server.CommandLine.Contains($ExpectedCommandFragment)) {
    throw 'Listener does not match the expected server; nothing stopped'
}
$oldId = $server.ProcessId
$oldCreated = $server.CreationDate
Stop-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 1
# Console wrappers can exit while their server survives. Check exact identity.
$remaining = Get-CimInstance Win32_Process -Filter "ProcessId = $oldId"
if ($remaining -and $remaining.CreationDate -eq $oldCreated) {
    Stop-Process -Id $oldId -ErrorAction Stop
}
# Stop-Process returns before Windows necessarily releases the listening socket.
# Wait for the observed owner; never stop a replacement or race a second bind.
$deadline = [DateTime]::UtcNow.AddSeconds(15)
do {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (@($listeners | Where-Object OwningProcess -ne $oldId).Count) {
        throw 'A different process claimed the port; nothing else stopped'
    }
    if ($listeners.Count -eq 0) { break }
    Start-Sleep -Milliseconds 100
} while ([DateTime]::UtcNow -lt $deadline)
if ($listeners.Count) { throw 'Original listener did not close within 15 seconds' }
Start-ScheduledTask -TaskName $TaskName
$deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
do {
    Start-Sleep -Milliseconds 500
    $new = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
} while ($new.Count -eq 0 -and [DateTime]::UtcNow -lt $deadline)
if ($new.Count -eq 0 -or $new.OwningProcess -contains $oldId) {
    throw 'New server listener was not observed'
}
@{status='restarted'; previous_pid=$oldId; listener_pid=$new[0].OwningProcess;
  time=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json
