# Optional machine-local adapter choice for start_all.bat. Only emit an IPv4 address.
# The selection file contains an adapter GUID, never a fixed DHCP address.
$ErrorActionPreference = 'Stop'
$selectionFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'tunnel_interface.txt'
if (-not (Test-Path -LiteralPath $selectionFile)) { exit 0 }

try {
    $selectedGuid = [guid]([IO.File]::ReadAllText($selectionFile).Trim())
    $adapters = @(Get-NetAdapter -Physical | Where-Object {
        [guid]$_.InterfaceGuid -eq $selectedGuid -and $_.Status -eq 'Up'
    })
    if ($adapters.Count -ne 1) { throw 'Selected tunnel network adapter is unavailable.' }
    $addresses = @(Get-NetIPAddress -InterfaceIndex $adapters[0].ifIndex -AddressFamily IPv4 |
        Where-Object { $_.AddressState -eq 'Preferred' -and -not $_.SkipAsSource -and $_.IPAddress -notlike '169.254.*' })
    if ($addresses.Count -lt 1) { throw 'Selected tunnel network adapter has no usable IPv4 address.' }
    [Console]::Out.WriteLine($addresses[0].IPAddress)
} catch {
    [Console]::Error.WriteLine('Tunnel adapter: ' + $_.Exception.Message)
    exit 1
}
