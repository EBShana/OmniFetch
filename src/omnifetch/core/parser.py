"""Title parser: split a raw release title into (work_key, Quality).

work_key is the work identity with all version information stripped
(resolution/source/codec/season/episode/year/group); Quality keeps what was
stripped so the task generator can rank candidates of one want.
"""
from __future__ import annotations

import re
import unicodedata

from .models import Quality

_EXT_RE = re.compile(
    r"\.(mkv|mp4|avi|ts|m2ts|wmv|flv|mov|mp3|flac|aac|zip|rar|7z|pdf|epub)$",
    re.IGNORECASE,
)
_RES_RE = re.compile(r"^(?P<res>(2160|1080|720|576|480|360)p|4k|uhd)$")
_SE_EP_RE = re.compile(r"^s(?P<season>\d{1,3})(?:e(?P<episode>\d{1,4}))?$", re.IGNORECASE)
_X_EP_RE = re.compile(r"^(?P<season>\d{1,2})x(?P<episode>\d{1,4})$", re.IGNORECASE)
_E_ONLY_RE = re.compile(r"^e(?P<episode>\d{1,4})$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_CRC_RE = re.compile(r"^[0-9a-f]{6,}$", re.IGNORECASE)
_AUDIO_RE = re.compile(r"^(aac|ac3|eac3|ddp?|dts|truehd|atmos|flac|mp3|opus|vorbis|\d\.\d)$")

_SOURCES = {
    "bluray", "blu-ray", "bdrip", "bdmv", "bd", "remux",
    "web-dl", "webdl", "webrip", "web",
    "hdtv", "tvrip", "pdtv", "dvdrip", "dvd", "cam", "ts", "tc", "ld",
}
_CODECS = {"x264", "x265", "h264", "h265", "hevc", "avc", "av1", "xvid", "divx", "vp9"}

_CJK_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cjk_num(text: str) -> int | None:
    if text in _CJK_DIGITS:
        return _CJK_DIGITS[text]
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CJK_DIGITS.get(left, 1) if left else 1
        ones = _CJK_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _consume_token(token: str, quality: Quality) -> bool:
    """Classify one token into ``quality``; return True if it was a version tag."""
    t = token.lower().strip().strip("()[]{}<>")
    if not t:
        return True

    m = _RES_RE.match(t)
    if m:
        quality.resolution = 2160 if t in {"4k", "uhd"} else int(t.rstrip("p"))
        return True
    if t in _SOURCES:
        quality.source = t
        return True
    if t in _CODECS:
        quality.codec = t
        return True
    if _AUDIO_RE.match(t):
        return True

    m = _SE_EP_RE.match(t)
    if m:
        quality.season = int(m.group("season"))
        if m.group("episode"):
            quality.episode = int(m.group("episode"))
        return True
    m = _X_EP_RE.match(t)
    if m:
        quality.season = int(m.group("season"))
        quality.episode = int(m.group("episode"))
        return True
    m = _E_ONLY_RE.match(t)
    if m:
        quality.episode = int(m.group("episode"))
        return True

    m = re.fullmatch(r"第([0-9]+)季", t)
    if m:
        quality.season = int(m.group(1))
        return True
    m = re.fullmatch(r"第([0-9]+)(?:集|话)", t)
    if m:
        quality.episode = int(m.group(1))
        return True
    m = re.fullmatch(r"第([一二三四五六七八九十]+)季", t)
    if m:
        quality.season = _cjk_num(m.group(1))
        return True
    m = re.fullmatch(r"第([一二三四五六七八九十]+)(?:集|话)", t)
    if m:
        quality.episode = _cjk_num(m.group(1))
        return True

    if _YEAR_RE.match(t):
        quality.year = int(t)
        return True

    # joined codec-group token, e.g. "x265-GRP"
    if "-" in t:
        head, _, tail = t.rpartition("-")
        if head in _CODECS and 2 <= len(tail) <= 20 and tail.isalnum():
            quality.codec = head
            quality.group = tail.lower()
            return True

    return False


def normalize_text(text: str) -> str:
    """NFKC + lowercase + whitespace collapse; used for normalized_title."""
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", text).strip()


def parse_title(raw_title: str) -> tuple[str, Quality]:
    """Return (work_key, quality) parsed from a release title."""
    quality = Quality()
    title = unicodedata.normalize("NFKC", raw_title).strip()
    title = _EXT_RE.sub("", title)

    def handle_bracket(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return " "
        if _CRC_RE.match(inner):  # CRC checksum, not a group tag
            return " "
        if not _consume_token(inner, quality):
            quality.group = inner.lower()  # first free-text bracket = release group
        return " "

    title = re.sub(r"\[([^\]]*)\]", handle_bracket, title)

    kept: list[str] = []
    for token in re.split(r"[\s._]+", title):
        if not token or token in {"-", "_", "~"}:
            continue
        if not _consume_token(token, quality):
            kept.append(token)

    return " ".join(kept).lower(), quality
