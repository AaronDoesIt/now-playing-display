# Adds this laptop's SSH public key to the Pi, so later logins need no password.
# The Pi asks for its password once; you type it yourself and nothing stores it.
#
#   powershell -ExecutionPolicy Bypass -File deploy\add-key-to-pi.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\add-key-to-pi.ps1 -User pi -PiHost 10.0.0.61
param(
    [string]$User = 'admin',
    [string]$PiHost = '10.0.0.61'
)

$pub = Join-Path $env:USERPROFILE '.ssh\id_ed25519.pub'
if (-not (Test-Path $pub)) {
    Write-Host "No public key at $pub" -ForegroundColor Red
    exit 1
}

Write-Host "Adding this laptop's key to $User@$PiHost."
Write-Host "Type the Pi's password for '$User' when asked (nothing shows while you type), then press Enter."
$remote = 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo KEY-ADDED for $(whoami) on $(hostname)'
Get-Content $pub | ssh -o StrictHostKeyChecking=accept-new "$User@$PiHost" $remote
if ($LASTEXITCODE -eq 0) {
    Write-Host 'Done. You can close this tab.' -ForegroundColor Green
} else {
    Write-Host "That did not work (exit $LASTEXITCODE). If the user name on the Pi is not '$User', tell Claude the right one." -ForegroundColor Yellow
}
