param(
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$targetPrefix = '185.149.145.12/32'
$vpnAliasPattern = 'happ-tun'

try {
    $vpnDefault = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
        Where-Object InterfaceAlias -EQ $vpnAliasPattern |
        Select-Object -First 1

    if (-not $vpnDefault) {
        exit 0
    }

    $physicalDefault = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
        Where-Object {
            $_.InterfaceAlias -ne $vpnAliasPattern -and
            $_.NextHop -ne '0.0.0.0'
        } |
        Sort-Object RouteMetric |
        Select-Object -First 1

    if (-not $physicalDefault) {
        throw 'No physical IPv4 default route was found.'
    }

    $existingRoute = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $targetPrefix -ErrorAction SilentlyContinue |
        Where-Object {
            $_.InterfaceIndex -eq $physicalDefault.InterfaceIndex -and
            $_.NextHop -eq $physicalDefault.NextHop
        } |
        Select-Object -First 1

    if ($existingRoute) {
        exit 0
    }

    if (-not $Elevated) {
        $arguments = @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', $PSCommandPath,
            '-Elevated'
        )
        $elevatedProcess = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -Wait -PassThru
        exit $elevatedProcess.ExitCode
    }

    New-NetRoute `
        -DestinationPrefix $targetPrefix `
        -InterfaceIndex $physicalDefault.InterfaceIndex `
        -NextHop $physicalDefault.NextHop `
        -RouteMetric 1 `
        -PolicyStore ActiveStore |
        Out-Null

    exit 0
}
catch {
    Write-Error "Failed to prepare Netdata route: $($_.Exception.Message)"
    exit 1
}
