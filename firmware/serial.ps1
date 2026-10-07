# Reset the E1001 and capture its boot log. Serial is UART0 on GPIO43/44
# behind the CH340, so this is the classic RTS-drives-EN auto-reset circuit.
param([string]$Port = "COM8", [int]$Seconds = 60)

$sp = New-Object System.IO.Ports.SerialPort $Port, 115200, 'None', 8, 'One'
$sp.ReadTimeout = 500
$sp.DtrEnable = $false
$sp.RtsEnable = $false
$sp.Open()

# EN low, then release -> boot into the app (IO0 left high, so not bootloader).
$sp.RtsEnable = $true
Start-Sleep -Milliseconds 150
$sp.RtsEnable = $false

$deadline = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $deadline) {
    try   { $line = $sp.ReadLine(); Write-Output $line.TrimEnd() }
    catch { }
}
$sp.Close()
