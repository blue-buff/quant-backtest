# List node processes with command line; optionally kill only the notify_bridge ones.
param([switch]$KillBridge)
$procs = Get-CimInstance Win32_Process -Filter "Name='node.exe'"
foreach ($p in $procs) {
    $cmd = $p.CommandLine
    if ($cmd -match 'notify_bridge') {
        Write-Output ("BRIDGE pid=" + $p.ProcessId + " cmd=" + $cmd.Substring(0, [Math]::Min(120, $cmd.Length)))
        if ($KillBridge) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; Write-Output ("  killed " + $p.ProcessId) }
    } else {
        Write-Output ("OTHER  pid=" + $p.ProcessId + " cmd=" + $cmd.Substring(0, [Math]::Min(120, $cmd.Length)))
    }
}
