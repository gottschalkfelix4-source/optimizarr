# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Stage 1 - build the web UI
# ---------------------------------------------------------------------------
FROM node:24-bookworm-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# ---------------------------------------------------------------------------
# Stage 2 - runtime
#
# jellyfin-ffmpeg is used rather than Debian's ffmpeg: it ships the Intel
# QSV/VAAPI stack pre-wired (oneVPL, iHD driver, SVT-AV1, libvmaf), which is
# exactly the combination this application needs and the hardest part to get
# right by hand.
# ---------------------------------------------------------------------------
FROM debian:trixie-slim

ARG JELLYFIN_FFMPEG_PACKAGE=jellyfin-ffmpeg7

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OPTIMIZARR_CONFIG_DIR=/config \
    OPTIMIZARR_TRANSCODE_DIR=/transcode \
    OPTIMIZARR_MEDIA_ROOT=/media \
    OPTIMIZARR_STATIC_DIR=/app/static \
    LIBVA_DRIVER_NAME=iHD \
    PUID=99 \
    PGID=100 \
    UMASK=002 \
    TZ=Europe/Berlin

# --- system packages -------------------------------------------------------
RUN set -eux; \
    # trixie uses deb822 sources - enable non-free in place rather than adding
    # a second list (which apt warns about as a duplicate).
    sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' \
        /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg tzdata gosu procps tini \
        python3 python3-pip \
        libva2 libva-drm2 vainfo \
        intel-media-va-driver-non-free \
        mesa-va-drivers; \
    \
    # --- jellyfin-ffmpeg (Intel QSV/VAAPI + SVT-AV1 + libvmaf) ---
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg arch=$(dpkg --print-architecture)] \
https://repo.jellyfin.org/debian trixie main" > /etc/apt/sources.list.d/jellyfin.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends ${JELLYFIN_FFMPEG_PACKAGE}; \
    ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg; \
    ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe; \
    \
    apt-get purge -y --auto-remove gnupg; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# --- python dependencies ---------------------------------------------------
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# --- application -----------------------------------------------------------
WORKDIR /app
COPY backend/app /app/app
COPY --from=frontend /build/dist /app/static
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/config", "/transcode", "/media"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

LABEL org.opencontainers.image.title="Optimizarr" \
      org.opencontainers.image.description="KI-gestuetzte AV1-Optimierung fuer Medienbibliotheken" \
      org.opencontainers.image.source="https://github.com/gottschalkfelix4-source/optimizarr" \
      org.opencontainers.image.licenses="MIT"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
