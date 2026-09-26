param([string]$TaskName, [string]$Python, [string]$Server, [string]$RestartScript, [int]$Port)
$ErrorActionPreference='Stop'
if(Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue){throw 'Fixture already exists'}
$action=New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless `"$Python`" `"$Server`" $Port"
$principal=New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal | Out-Null
try {
    Start-ScheduledTask -TaskName $TaskName
    $until=[DateTime]::UtcNow.AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 250
        $listener=Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    } while(-not $listener -and [DateTime]::UtcNow -lt $until)
    if(-not $listener){throw 'Fixture did not start'}
    & $RestartScript -TaskName $TaskName -Port $Port -ExpectedCommandFragment $Server
} finally {
    try {
        Stop-ScheduledTask -TaskName $TaskName
        $listener=Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if($listener){
            $p=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
            if($p -and $p.CommandLine -and $p.CommandLine.Contains($Server)){Stop-Process -Id $p.ProcessId}
        }
    } finally {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
}
