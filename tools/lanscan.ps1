# ARP scan of the home /24 straight out of the Wi-Fi adapter (SendARP with an explicit source address),
# so the result does not depend on whether a VPN on this PC is carrying the LAN traffic.
param([string]$Prefix = '10.0.0.', [string]$Src = '10.0.0.131', [int]$Passes = 3)

$code = @'
using System;
using System.Net;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Collections.Concurrent;
public static class ArpScan {
    [DllImport("iphlpapi.dll", ExactSpelling = true)]
    static extern int SendARP(uint DestIP, uint SrcIP, byte[] pMacAddr, ref uint PhyAddrLen);
    public static string[] Scan(string prefix, string src) {
        ThreadPool.SetMinThreads(96, 96);
        var bag = new ConcurrentBag<string>();
        uint s = BitConverter.ToUInt32(IPAddress.Parse(src).GetAddressBytes(), 0);
        Parallel.For(1, 255, new ParallelOptions { MaxDegreeOfParallelism = 96 }, i => {
            var ip = IPAddress.Parse(prefix + i);
            uint d = BitConverter.ToUInt32(ip.GetAddressBytes(), 0);
            byte[] mac = new byte[6];
            uint len = 6;
            if (SendARP(d, s, mac, ref len) == 0 && len == 6)
                bag.Add(ip + " " + BitConverter.ToString(mac).Replace("-", ":"));
        });
        return bag.ToArray();
    }
}
'@
Add-Type -TypeDefinition $code -Language CSharp

$found = @{}
for ($p = 1; $p -le $Passes; $p++) {
    foreach ($line in [ArpScan]::Scan($Prefix, $Src)) {
        $ip, $mac = $line -split ' '
        $found[$ip] = $mac
    }
}

# Raspberry Pi address blocks (Raspberry Pi Foundation / Raspberry Pi Trading / Raspberry Pi Ltd)
$piOui = '28:CD:C1', '2C:CF:67', '88:A2:9E', 'B8:27:EB', 'D8:3A:DD', 'DC:A6:32', 'E4:5F:01'

$rows = foreach ($ip in $found.Keys) {
    $mac = $found[$ip]
    $oui = $mac.Substring(0, 8)
    $first = [Convert]::ToInt32($mac.Substring(0, 2), 16)
    $name = ''
    try {
        $r = Resolve-DnsName -Name $ip -Type PTR -Server 10.0.0.1 -DnsOnly -QuickTimeout -ErrorAction Stop
        $name = ($r | Where-Object { $_.NameHost } | Select-Object -First 1).NameHost
    } catch { }
    [pscustomobject]@{
        IP     = $ip
        Sort   = [int]($ip -split '\.')[-1]
        MAC    = $mac
        Kind   = if ($piOui -contains $oui) { 'RASPBERRY PI' } elseif ($first -band 2) { 'private/random MAC' } else { '' }
        Name   = $name
    }
}
$rows | Sort-Object Sort | Format-Table IP, MAC, Kind, Name -AutoSize
"hosts answering ARP: $($found.Count)"
