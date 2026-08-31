#!/bin/sh
# Intel-GPU-Diagnose fuer Optimizarr.
#
# Faellt das Hardware-Encoding auf die CPU zurueck, obwohl die Erkennung die GPU
# als einsatzbereit meldet, liegt es an einer Kombination, die der kurze
# Erkennungstest nicht abdeckt. Dieses Skript trennt die Kandidaten einzeln auf
# und zeigt, welcher davon traegt.
#
# Aufruf auf dem Unraid-Server (Container-Name ggf. anpassen):
#   curl -sL https://raw.githubusercontent.com/gottschalkfelix4-source/optimizarr/main/scripts/intel-diagnose.sh | docker exec -i Optimizarr sh
#
set -u

FF=${FF:-/usr/lib/jellyfin-ffmpeg/ffmpeg}
FP=${FP:-/usr/lib/jellyfin-ffmpeg/ffprobe}
D=${D:-/dev/dri/renderD128}
QSV="-init_hw_device qsv=hw,child_device=$D -filter_hw_device hw"
SRC="-f lavfi -i testsrc2=size=1920x1080:rate=24 -frames:v 120"

run() {
  name="$1"; shift
  printf '  %-44s' "$name"
  if "$@" >/tmp/diag.log 2>&1; then
    echo 'OK'
    return 0
  fi
  echo 'FEHLER'
  grep -iE 'error|unsupported|invalid|failed|cannot|impossible|not (implemented|supported)' /tmp/diag.log \
    | grep -viE 'error_?rate|last message|deprecated' | head -2 | sed 's/^/          /'
  return 1
}

echo "Geraet: $D"
if [ ! -e "$D" ]; then
  echo '  FEHLT - /dev/dri ist im Container nicht sichtbar. Im Unraid-Template'
  echo '  als Device eintragen. Ohne das laeuft alles auf der CPU.'
  exit 1
fi
echo

echo '=== 1. Der eigentliche Vergleich ==='
run 'ALT (mit extbrc + look_ahead)' \
  $FF -v error -y $QSV $SRC -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -preset 4 -g 120 -low_power 1 \
  -extbrc 1 -look_ahead_depth 40 -f null -
alt=$?

run 'NEU (ohne beide)' \
  $FF -v error -y $QSV $SRC -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -preset 4 -g 120 -low_power 1 -f null -
neu=$?

if [ $alt -ne 0 ] && [ $neu -eq 0 ]; then
  echo
  echo '  >>> Gefunden: extbrc und/oder look_ahead_depth sind die Ursache.'
  echo '      Genau die entfernt die neue Version.'
fi
echo

echo '=== 2. Welcher der beiden war es? ==='
run 'nur extbrc' \
  $FF -v error -y $QSV $SRC -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -low_power 1 -extbrc 1 -f null -
run 'nur look_ahead_depth' \
  $FF -v error -y $QSV $SRC -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -low_power 1 -look_ahead_depth 40 -f null -
run 'ohne low_power' \
  $FF -v error -y $QSV $SRC -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -f null -
run '8-bit statt 10-bit' \
  $FF -v error -y $QSV $SRC -vf format=nv12,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -low_power 1 -f null -
echo

echo '=== 3. Der Weg mit GPU-Dekodierung ==='
F=$(find /media -type f \( -name '*.mkv' -o -name '*.mp4' \) -size +200M 2>/dev/null | head -1)
if [ -z "$F" ]; then
  echo '  Keine Datei unter /media gefunden - erzeuge eine Testdatei.'
  F=/tmp/diag-src.mp4
  $FF -v error -y -f lavfi -i testsrc2=size=1920x1080:rate=24 -frames:v 240 \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p "$F" 2>/dev/null
else
  echo "  Datei: $(basename "$F")"
  $FP -v error -select_streams v:0 -show_entries stream=codec_name,pix_fmt,width,height \
    -of csv=p=0 "$F" 2>/dev/null | sed 's/^/          /'
fi

run 'ALT: kein Filter (verliert 10 Bit)' \
  $FF -v error -y $QSV -hwaccel qsv -hwaccel_output_format qsv -hwaccel_device hw \
  -i "$F" -t 20 -map 0:v:0 -an -sn -c:v av1_qsv -global_quality 30 -low_power 1 -f null -

run 'NEU: vpp_qsv + extra_hw_frames' \
  $FF -v error -y $QSV -hwaccel qsv -hwaccel_output_format qsv -hwaccel_device hw \
  -extra_hw_frames 16 -i "$F" -t 20 -map 0:v:0 -an -sn \
  -vf vpp_qsv=format=p010le -c:v av1_qsv -global_quality 30 -low_power 1 -f null -

run 'Rueckfall: CPU-Dekodierung, GPU-Encode' \
  $FF -v error -y $QSV -i "$F" -t 20 -map 0:v:0 -an -sn \
  -vf format=p010le,hwupload=extra_hw_frames=64 \
  -c:v av1_qsv -global_quality 30 -low_power 1 -f null -
echo

echo '=== 4. VAAPI als Alternative ==='
run 'av1_vaapi 10-bit' \
  $FF -v error -y -vaapi_device $D $SRC -vf format=p010le,hwupload \
  -c:v av1_vaapi -qp 30 -f null -
echo

echo '=== 5. Tonspuren ==='
echo '  (Faellt hier etwas aus, sieht es im Job-Protokoll wie ein GPU-Problem aus,'
echo '   weil ffmpeg bei einem Fehler die ganze Verarbeitung abbricht.)'
if [ -n "${F:-}" ] && [ -f "$F" ]; then
  $FP -v error -select_streams a \
    -show_entries stream=index,codec_name,channels,channel_layout,sample_rate \
    -of csv=p=0 "$F" 2>/dev/null | sed 's/^/  /'
  run 'Alle Tonspuren nach Opus' \
    $FF -v error -y -i "$F" -t 10 -map 0:a -c:a libopus -b:a 288k -f null -
  run 'Alle Untertitel kopieren' \
    $FF -v error -y -i "$F" -t 10 -map 0:s? -c:s copy -f matroska /dev/null
else
  echo '  (keine Datei zum Pruefen)'
fi
[ "${F:-}" = /tmp/diag-src.mp4 ] && rm -f "$F"
echo

echo 'Fertig. Was oben mit OK durchlaeuft, kann Optimizarr nutzen.'
