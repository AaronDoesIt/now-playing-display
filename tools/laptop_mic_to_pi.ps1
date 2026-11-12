# Use this laptop's microphone as the Now Playing display's ear, for testing.
#
# It captures the laptop's microphone and sends the sound, as raw 16-bit 16 kHz mono
# audio, to the Pi in the TV (UDP port 5004 on your home network, nowhere else).
# The Pi decides whether it is music and looks the song up.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\laptop_mic_to_pi.ps1
#
# THE MICROPHONE IS LIVE WHILE THIS RUNS. Stop it with Ctrl-C (or close the tab).
# With nothing sending, the Pi hears nothing and the panel shows the idle pictures.
param(
    [string]$PiHost = '10.0.0.61',
    [int]$Port = 5004,
    [string]$Device = '',     # a DirectShow audio device name; empty = the first microphone
    [int]$GainDb = 15         # the laptop's microphone array is quiet: room music read about
                              # -44 dB without gain, near the daemon's -50 dB silence line
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$ffmpeg = & $python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
if (-not $ffmpeg -or -not (Test-Path $ffmpeg)) {
    Write-Host 'ffmpeg was not found in the project''s Python environment.' -ForegroundColor Red
    exit 1
}

if (-not $Device) {
    # ffmpeg prints the device list on its error stream; cmd merges the two streams for us.
    $listing = cmd /c "`"$ffmpeg`" -hide_banner -list_devices true -f dshow -i dummy 2>&1"
    $name = $null
    foreach ($line in $listing) {
        if ($line -match '"(.+)"\s+\(audio\)') { $name = $Matches[1]; continue }
        if ($name -and $line -match 'Alternative name "(.+)"') { $Device = $Matches[1]; break }
    }
    if (-not $Device) {
        Write-Host 'No microphone was found on this laptop.' -ForegroundColor Red
        exit 1
    }
    Write-Host "Microphone: $name"
}

Write-Host "Sending this laptop's microphone to ${PiHost}:$Port with $GainDb dB of gain. The microphone is LIVE."
Write-Host 'Stop with Ctrl-C.' -ForegroundColor Yellow
& $ffmpeg -hide_banner -loglevel warning -stats -f dshow -audio_buffer_size 50 -i "audio=$Device" `
    -af "volume=${GainDb}dB" -ac 1 -ar 16000 -acodec pcm_s16le -f s16le "udp://${PiHost}:${Port}?pkt_size=1024"
Write-Host "The microphone stream has stopped (exit $LASTEXITCODE)."
