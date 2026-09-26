# Invoke through RunSubmit. Verify no unrelated work before server maintenance.
param(
    [Parameter(Mandatory=$true)][string]$TaskName,
    [Parameter(Mandatory=$true)][int]$Port,
    [Parameter(Mandatory=$true)][string]$ExpectedCommandFragment
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
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw 'Listener remained or changed; refusing a second server'
}
Start-ScheduledTask -TaskName $TaskName
$deadline = [DateTime]::UtcNow.AddSeconds(25)
do {
    Start-Sleep -Milliseconds 500
    $new = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
} while ($new.Count -eq 0 -and [DateTime]::UtcNow -lt $deadline)
if ($new.Count -eq 0 -or $new.OwningProcess -contains $oldId) {
    throw 'New server listener was not observed'
}
@{status='restarted'; previous_pid=$oldId; listener_pid=$new[0].OwningProcess;
  time=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json
