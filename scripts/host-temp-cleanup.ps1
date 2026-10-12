$ErrorActionPreference = 'Continue'
$t = Join-Path $env:LOCALAPPDATA 'Temp'
$before = (Get-PSDrive C).Free
$cut = (Get-Date).AddDays(-2)
$crashes = Join-Path $t 'wsl-crashes'
if (Test-Path $crashes) {
    Get-ChildItem $crashes -File -Filter *.dmp -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cut } |
        ForEach-Object { try { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop } catch {} }
}
Get-ChildItem $t -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$' -and $_.LastWriteTime -lt $cut } |
    ForEach-Object {
        $files = @(Get-ChildItem $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue)
        $other = @($files | Where-Object { $_.Name -ne 'swap.vhdx' })
        if ($other.Count -eq 0) {
            try { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction Stop } catch {}
        }
    }
$freed = [math]::Round(((Get-PSDrive C).Free - $before) / 1GB, 2)
"host-temp-cleanup freed_gb=$freed free_gb=$([math]::Round((Get-PSDrive C).Free/1GB,1))"
