#!/bin/sh
# Intel-GPU-Diagnose fuer Optimizarr.
#
# Faellt das Hardware-Encoding auf die CPU zurueck, obwohl die Erkennung die GPU
# als einsatzbereit meldet, liegt es meist an einer Kombination, die der kurze
# Erkennungstest gar nicht abdeckt: 10-Bit-Ausgabe, GPU-Decoding, oder ein
# Encoder-Parameter, den genau dieser Chip nicht mag.
#
# Dieses Skript probiert die Varianten einzeln durch und zeigt, welche traegt.
#
# Aufruf auf dem Unraid-Server (Container-Name ggf. anpassen):
#   curl -sL https://raw.githubusercontent.com/gottschalkfelix4-source/optimizarr/main/scripts/intel-diagnose.sh | docker exec -i Optimizarr sh
#
set -u

FF=/usr/lib/jellyfin-ffmpeg/ffmpeg
D=/dev/dri/renderD128
SRC="-f lavfi -i testsrc2=size=1920x1080:rate=24 -frames:v 30"

run() {
  name="$1"; shift
  printf '%-46s' "$name"
  if "$@" >/tmp/diag.log 2>&1; then
    echo 'OK'
  else
    echo 'FEHLER'
    grep -iE 'error|unsupported|invalid|failed|not (implemented|supported)|no such' /tmp/diag.log \
      | grep -viE 'error_?rate|last message' | head -2 | sed 's/^/        /'
  fi
}

echo '--- 1. Zielformat 10-bit (so wie Optimizarr es macht) ---'
run 'A  p010le, minimal' \
  $FF -v error -y -init_hw_device qsv=hw,child_device=$D -filter_hw_device hw \
  $SRC -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -f null -

run 'B  p010le + low_power 1' \
  $FF -v error -y -init_hw_device qsv=hw,child_device=$D -filter_hw_device hw \
  $SRC -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -low_power 1 -f null -

run 'C  p010le + extbrc + look_ahead' \
  $FF -v error -y -init_hw_device qsv=hw,child_device=$D -filter_hw_device hw \
  $SRC -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -low_power 1 -extbrc 1 -look_ahead_depth 40 -f null -

run 'D  p010le + preset 4 + g 120' \
  $FF -v error -y -init_hw_device qsv=hw,child_device=$D -filter_hw_device hw \
  $SRC -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -preset 4 -g 120 -f null -

echo
echo '--- 2. Zum Vergleich: 8-bit ---'
run 'E  nv12, minimal' \
  $FF -v error -y -init_hw_device qsv=hw,child_device=$D -filter_hw_device hw \
  $SRC -vf format=nv12,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -f null -

echo
echo '--- 3. VAAPI als Alternative ---'
run 'F  av1_vaapi 10-bit' \
  $FF -v error -y -vaapi_device $D $SRC -vf format=p010le,hwupload \
  -c:v av1_vaapi -qp 30 -f null -

run 'G  av1_vaapi 8-bit' \
  $FF -v error -y -vaapi_device $D $SRC -vf format=nv12,hwupload \
  -c:v av1_vaapi -qp 30 -f null -

echo
echo '--- 4. Echte Datei mit GPU-Decoding (der eigentliche Verdaechtige) ---'
F=$(find /media -type f -name '*.mkv' -size +200M 2>/dev/null | head -1)
if [ -z "$F" ]; then
  echo '  keine .mkv unter /media gefunden - Abschnitt uebersprungen'
else
  echo "  Datei: $F"
  /usr/lib/jellyfin-ffmpeg/ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,pix_fmt,width,height -of default=nw=1 "$F" | sed 's/^/  /'
  echo

  run 'H  hwaccel qsv, KEIN -vf (aktueller Code)' \
    $FF -v error -y -init_hw_device qsv=hw,child_device=$D -filter_hw_device hw \
    -hwaccel qsv -hwaccel_output_format qsv -hwaccel_device hw -i "$F" -t 20 \
    -map 0:v:0 -an -sn -c:v av1_qsv -global_quality 30 -preset 4 -g 120 \
    -low_power 1 -extbrc 1 -look_ahead_depth 40 -f null -

  run 'I  hwaccel qsv + vpp_qsv=format=p010le' \
    $FF -v error -y -init_hw_device qsv=hw,child_device=$D -filter_hw_device hw \
    -hwaccel qsv -hwaccel_output_format qsv -hwaccel_device hw -i "$F" -t 20 \
    -map 0:v:0 -an -sn -vf vpp_qsv=format=p010le \
    -c:v av1_qsv -global_quality 30 -f null -

  run 'J  hwaccel qsv + vpp_qsv, ohne -hwaccel_device' \
    $FF -v error -y -init_hw_device qsv=hw,child_device=$D -filter_hw_device hw \
    -hwaccel qsv -hwaccel_output_format qsv -i "$F" -t 20 \
    -map 0:v:0 -an -sn -vf vpp_qsv=format=p010le \
    -c:v av1_qsv -global_quality 30 -f null -

  run 'K  Software-Decoding, dann hwupload' \
    $FF -v error -y -init_hw_device qsv=hw,child_device=$D -filter_hw_device hw \
    -i "$F" -t 20 -map 0:v:0 -an -sn \
    -vf format=p010le,hwupload=extra_hw_frames=64 \
    -c:v av1_qsv -global_quality 30 -f null -
fi

echo
echo '--- fertig ---'
