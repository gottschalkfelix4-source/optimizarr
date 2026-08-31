#!/usr/bin/env bash
# Container entrypoint.
#
# Unraid expects containers to write files as 99:100 (nobody:users), so the app
# drops from root to PUID:PGID.  The one thing that must happen before dropping
# is joining whatever group owns /dev/dri - otherwise the render node is
# invisible to the unprivileged user and every hardware encode fails with a
# confusing permission error.
set -euo pipefail

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"
APP_USER="optimizarr"

log() { printf '[entrypoint] %s\n' "$*"; }

umask "${UMASK}"

# --- writable directories ---------------------------------------------------
for dir in "${OPTIMIZARR_CONFIG_DIR:-/config}" "${OPTIMIZARR_TRANSCODE_DIR:-/transcode}"; do
    mkdir -p "${dir}"
done

# Leftovers from a container that was killed mid-encode.
find "${OPTIMIZARR_TRANSCODE_DIR:-/transcode}" -maxdepth 1 -name 'optimizarr-*' -mmin +60 \
    -delete 2>/dev/null || true

# --- run as root when asked (PUID=0) ---------------------------------------
if [ "${PUID}" = "0" ]; then
    log "running as root (PUID=0)"
    exec "$@"
fi

# --- create the runtime user ------------------------------------------------
if ! getent group "${PGID}" >/dev/null 2>&1; then
    groupadd -o -g "${PGID}" "${APP_USER}"
fi
GROUP_NAME="$(getent group "${PGID}" | cut -d: -f1)"

if ! getent passwd "${PUID}" >/dev/null 2>&1; then
    useradd -o -u "${PUID}" -g "${PGID}" -M -d /config -s /usr/sbin/nologin "${APP_USER}"
fi
USER_NAME="$(getent passwd "${PUID}" | cut -d: -f1)"

# --- hardware access --------------------------------------------------------
if [ -d /dev/dri ]; then
    for node in /dev/dri/*; do
        [ -e "${node}" ] || continue
        node_gid="$(stat -c '%g' "${node}")"
        [ "${node_gid}" = "0" ] && continue
        if ! getent group "${node_gid}" >/dev/null 2>&1; then
            groupadd -o -g "${node_gid}" "render_${node_gid}" 2>/dev/null || true
        fi
        node_group="$(getent group "${node_gid}" | cut -d: -f1)"
        if [ -n "${node_group}" ]; then
            usermod -aG "${node_group}" "${USER_NAME}" 2>/dev/null || true
            log "granted ${USER_NAME} access to ${node} (group ${node_group}/${node_gid})"
        fi
    done
else
    log "no /dev/dri present - encoding will run on the CPU"
fi

# --- ownership --------------------------------------------------------------
# Only the container's own directories, never the media library.
chown -R "${PUID}:${PGID}" "${OPTIMIZARR_CONFIG_DIR:-/config}" 2>/dev/null || true
chown "${PUID}:${PGID}" "${OPTIMIZARR_TRANSCODE_DIR:-/transcode}" 2>/dev/null || true

log "starting as ${USER_NAME}:${GROUP_NAME} (${PUID}:${PGID}), umask ${UMASK}"
exec gosu "${PUID}:${PGID}" "$@"
