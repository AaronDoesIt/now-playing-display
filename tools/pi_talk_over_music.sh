#!/usr/bin/env bash
# Test sound for the Pi's "is the song really over?" check: a music station with a talk station
# mixed over it part of the time, sent to the daemon's network audio input (UDP 5004).
#
# While only the music plays, the music detector hears music and the song is named as usual.
# When the talk comes in on top, the detector says "not music" although the song is still
# playing, which is what a rap verse or a quiet passage does through a room microphone. The
# daemon should then look the sound up once, find the song, and keep it on the display
# ("still playing: the music detector heard no music ..." in its log) instead of going idle.
#
# Run it ON THE PI, and stop it with Ctrl-C (or kill the ffmpeg it starts):
#
#   bash pi_talk_over_music.sh [music volume] [talk volume] [seconds of music alone] [cycle length]
#
# Defaults: music 0.25, talk 1.0, 70 s of music alone in every 150 s.
# Nothing is installed and nothing keeps running after it is stopped.
set -u
MUSIC="${NP_MUSIC_URL:-http://ice1.somafm.com/seventies-128-mp3}"
TALK="${NP_TALK_URL:-http://stream.live.vc.bbcmedia.co.uk/bbc_world_service}"
MV="${1:-0.25}"
TV="${2:-1.0}"
ALONE="${3:-70}"
CYCLE="${4:-150}"
exec ffmpeg -hide_banner -nostdin -loglevel error \
    -i "$MUSIC" -i "$TALK" \
    -filter_complex "[0:a]aresample=16000,volume=${MV}[m];[1:a]aresample=16000,volume='if(lt(mod(t,${CYCLE}),${ALONE}),0,${TV})':eval=frame[t];[m][t]amix=inputs=2:normalize=0[o]" \
    -map "[o]" -ac 1 -ar 16000 -f s16le "udp://127.0.0.1:5004?pkt_size=1024"
