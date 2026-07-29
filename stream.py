from __future__ import annotations

import logging

import aiohttp

from .constants import PLAY_ORIGIN, USER_AGENT

logger = logging.getLogger("SoopDropsMiner.stream")


def _resolve_url(base: str, chunk: str) -> str:
    if chunk.startswith("http"):
        return chunk
    root = base.rsplit("/", 1)[0]
    return f"{root}/{chunk}"


async def head_latest_segment(
    session: aiohttp.ClientSession,
    playlist_url: str,
    *,
    bjid: str,
    low_bandwidth_mode: bool = True,
) -> bool:
    # This is a diagnostics-only helper.  The default low-bandwidth policy must
    # never fetch a playlist or touch a media segment URL.
    if low_bandwidth_mode:
        logger.debug("低流量模式已阻止 HLS playlist/segment 探测")
        return False
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"{PLAY_ORIGIN}/{bjid}",
    }
    try:
        async with session.get(playlist_url, headers=headers) as resp:
            if resp.status != 200:
                return False
            text = await resp.text()
    except aiohttp.ClientError:
        return False

    if text.lstrip().startswith("<?xml") or "MissingKey" in text:
        return False

    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return False

    chunk = lines[-1]
    if chunk == "#EXT-X-ENDLIST" and len(lines) >= 2:
        chunk = lines[-2]

    if chunk.endswith(".m3u8") or "m3u8" in chunk:
        nested_url = _resolve_url(playlist_url, chunk)
        return await head_latest_segment(
            session,
            nested_url,
            bjid=bjid,
            low_bandwidth_mode=False,
        )

    seg_url = _resolve_url(playlist_url, chunk)
    try:
        async with session.head(seg_url, headers=headers) as resp:
            return resp.status == 200
    except aiohttp.ClientError:
        return False
