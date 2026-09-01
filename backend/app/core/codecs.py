"""Video codec names: one spelling in, one spelling out.

ffprobe reports a single short name per codec (``hevc``, ``mpeg2video``,
``msmpeg4v3``), but people write the same codec half a dozen ways - H.265,
h265, x265, HEVC.  A settings field that only matched ffprobe's spelling would
silently do nothing for anyone who typed the name they know, which is worse
than rejecting the input: the list looks accepted and files keep showing up as
candidates.

So everything that compares codecs goes through :func:`normalise` first, and
the UI shows :func:`label` instead of the raw name.  Unknown codecs are not an
error - they pass through lowercased, so a codec this table has never heard of
can still be excluded by typing its ffprobe name.
"""
from __future__ import annotations

# The tail of the skip reason written for a codec exclusion.  It is the marker
# that tells a file skipped by this setting apart from one skipped because it
# was too small or already lean - so lifting the exclusion can bring exactly
# those files back without re-analysing the rest of the library.
EXCLUSION_REASON = "ist in den Einstellungen ausgeschlossen."

# Canonical name -> how it is written in the UI.
LABELS: dict[str, str] = {
    "h264": "H.264 / AVC",
    "hevc": "HEVC / H.265",
    "av1": "AV1",
    "vvc": "VVC / H.266",
    "vp9": "VP9",
    "vp8": "VP8",
    "mpeg2video": "MPEG-2",
    "mpeg1video": "MPEG-1",
    "mpeg4": "MPEG-4 ASP (Xvid, DivX)",
    "msmpeg4v3": "MS MPEG-4 v3 (DivX 3)",
    "vc1": "VC-1",
    "wmv3": "WMV 9",
    "wmv2": "WMV 8",
    "theora": "Theora",
    "prores": "ProRes",
    "dnxhd": "DNxHD",
    "mjpeg": "MJPEG",
    "huffyuv": "HuffYUV",
    "ffv1": "FFV1",
    "rv40": "RealVideo",
}

# Everything people actually type, mapped onto the canonical name.
_ALIASES: dict[str, str] = {
    # H.264
    "avc": "h264", "avc1": "h264", "h.264": "h264", "x264": "h264", "mpeg4avc": "h264",
    # HEVC - the one this table exists for
    "h265": "hevc", "h.265": "hevc", "x265": "hevc", "hvc1": "hevc", "hev1": "hevc",
    "hevc10": "hevc", "h265hevc": "hevc",
    # AV1
    "av01": "av1", "aom": "av1", "aomav1": "av1", "svtav1": "av1", "libsvtav1": "av1",
    "av1qsv": "av1", "av1vaapi": "av1", "libaomav1": "av1",
    # VVC
    "h266": "vvc", "h.266": "vvc", "vvc1": "vvc",
    # VP8/9
    "vp09": "vp9", "vp08": "vp8", "libvpxvp9": "vp9",
    # MPEG family
    "mpeg2": "mpeg2video", "mpeg-2": "mpeg2video", "h262": "mpeg2video", "h.262": "mpeg2video",
    "mpeg1": "mpeg1video",
    "xvid": "mpeg4", "divx": "mpeg4", "mp4v": "mpeg4", "mpeg4asp": "mpeg4", "divx5": "mpeg4",
    "div3": "msmpeg4v3", "divx3": "msmpeg4v3",
    # Microsoft
    "vc-1": "vc1", "wvc1": "vc1", "wmv": "wmv3", "wmv9": "wmv3", "wmv8": "wmv2",
}


def normalise(name: str) -> str:
    """ffprobe's name for whatever the user (or a probe) called this codec."""
    key = (name or "").strip().lower()
    if not key:
        return ""
    # Strip the punctuation people sprinkle in: "H.265", "VC-1", "MPEG 2".
    stripped = key.replace(".", "").replace("-", "").replace("_", "").replace(" ", "")
    if key in LABELS:
        return key
    if stripped in LABELS:
        return stripped
    return _ALIASES.get(stripped, key)


def label(name: str) -> str:
    """Display name for a codec - falls back to the raw name in upper case."""
    canonical = normalise(name)
    return LABELS.get(canonical, canonical.upper())


def normalise_list(names: list[str]) -> list[str]:
    """Clean a user-entered exclusion list: normalised, deduped, order kept."""
    out: list[str] = []
    for name in names:
        canonical = normalise(name)
        if canonical and canonical not in out:
            out.append(canonical)
    return out


def spellings(name: str) -> list[str]:
    """Every stored spelling that means this codec.

    ffprobe is consistent, but a database filled by different ffmpeg builds can
    hold ``av01`` next to ``av1``.  SQL comparisons use this so a query can stay
    in the database instead of loading every row to normalise it in Python.
    """
    canonical = normalise(name)
    if not canonical:
        return []
    out = [canonical]
    out += [alias for alias, target in _ALIASES.items() if target == canonical]
    return out


def is_excluded(codec: str, skip_codecs: list[str]) -> bool:
    """Does this codec match the exclusion list, whatever spelling either uses?"""
    canonical = normalise(codec)
    if not canonical:
        return False
    return canonical in {normalise(c) for c in skip_codecs}
