$interfaces = [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()

foreach ($interface in $interfaces) {
    if ($interface.OperationalStatus -ne 'Up') { continue }
    if ($interface.NetworkInterfaceType -notin @('Ethernet', 'Wireless80211')) { continue }

    $properties = $interface.GetIPProperties()
    $ipv4Gateway = @($properties.GatewayAddresses | Where-Object {
        $_.Address.AddressFamily -eq 'InterNetwork'
    })
    if ($ipv4Gateway.Count -eq 0) { continue }

    foreach ($address in $properties.UnicastAddresses) {
        if ($address.Address.AddressFamily -ne 'InterNetwork') { continue }
        $ip = $address.Address.IPAddressToString
        if ($ip.StartsWith('169.254.')) { continue }
        Write-Output $ip
    }
}
