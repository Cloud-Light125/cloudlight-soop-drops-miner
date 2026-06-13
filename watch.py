from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

import aiohttp

from .constants import (
    GATHER_URL,
    HTTP_CONNECT_TIMEOUT,
    HTTP_TOTAL_TIMEOUT,
    PLAY_ORIGIN,
    SEP,
    USER_AGENT,
)
from .models import LiveChannel


def encode_a(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        parts.append(f"{key}{SEP}={SEP}{value}")
    return f"{SEP}&{SEP}".join(parts)


def compute_hash(payload: dict[str, str]) -> str:
    raw = "|".join(
        [
            payload["sv"],
            payload["ns"],
            payload["ver"],
            payload["tm"],
            payload["ht"],
            payload["cs"],
            payload["uid"],
            payload["a"],
            "afreeca",
        ]
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class WatchHeartbeat:
    """模拟 play.sooplive.com 播放器 CSTATUS 心跳。"""

    def __init__(
        self,
        uid: str,
        channel: LiveChannel,
        *,
        center_ip: str = "110.10.76.218",
        center_port: str = "19000",
    ):
        self.uid = uid
        self.channel = channel
        self.center_ip = center_ip
        self.center_port = center_port
        self.uniq_key = str(secrets.randbelow(10**17)).zfill(17)
        self.guid = secrets.token_hex(16).upper()
        self.uuid = secrets.token_hex(16)
        self.buffer_count = 0
        self._tick = 0
        self._session_start_ms = int(time.time() * 1000)
        self._last_buffer_ms = self._session_start_ms

    def switch_channel(
        self,
        channel: LiveChannel,
        *,
        center_ip: str | None = None,
        center_port: str | None = None,
    ) -> None:
        self.channel = channel
        self.buffer_count = 0
        self._tick = 0
        if center_ip:
            self.center_ip = center_ip
        if center_port:
            self.center_port = center_port
        now = int(time.time() * 1000)
        self._session_start_ms = now
        self._last_buffer_ms = now

    def _base_fields(self, *, m_type: str = "B", s_type: str = "2", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        self.buffer_count += 1
        segment_ms = max(500, min(now_ms - self._last_buffer_ms, 8000))
        self._last_buffer_ms = now_ms

        fields: dict[str, Any] = {
            "m_type": m_type,
            "s_type": s_type,
            "c_num": "0",
            "quality": "0",
            "bps": "500",
            "bj": self.channel.user_id,
            "bno": self.channel.broad_no or "0",
            "pbno": "0",
            "trans": "H",
            "p_ver": "2",
            "os": "win",
            "os_ver": "Windows 10",
            "resolution": "1920X1080",
            "dev_type": "30",
            "set_bps": "16000",
            "uniq_key": self.uniq_key,
            "guid": self.guid,
            "user_agent": "Chrome",
            "build_ver": "1779932790862",
            "lowlatency": "0",
            "agent_full": USER_AGENT,
            "uuid": self.uuid,
            "ad_uuid": "",
            "is_active_browser_tab": "true",
            "subscribe": "0",
            "sub_view_type": "non_sub",
            "is_chat_open": "false",
            "bw_full_version": "120.0.0.0",
            "is_iframeapi": "false",
            "join_cc": "156",
            "geo_cc": "CN",
            "geo_rc": "GD",
            "acpt_lang": "zh_CN",
            "svc_lang": "zh_CN",
            "buffer_count": str(self.buffer_count),
            "bufferLeft": "0.080",
            "abuse": "0",
            "join_api": "0",
            "api_center": "0",
            "center_seed": "0",
            "ad_sdk": "0",
            "ad_view": "0",
            "seed_play": "0",
            "seed_change_cnt": "0",
            "broad_info_type": "normal",
            "total": str(segment_ms),
            "is_adaptive": "false",
            "is_support_adaptive": "true",
            "is_clearmode": "false",
            "random_nickname_color": "false",
            "is_hidden": "false",
            "parent_session_key": "",
            "center_ip": self.center_ip,
            "center_port": self.center_port,
            "stream_protocol": "",
            "used_memory": str(110_000_000 + self.buffer_count * 1000),
            "total_memory": str(120_000_000 + self.buffer_count * 1000),
            "memory_limit": "4294967296",
            "last_buffer_time": str(now_ms),
            "buffer_start_time": str(self._session_start_ms),
            "ad_total": "0",
            "total_correction_value": "0",
            "player_key_count": "1",
            "cdn_src": "gcp_cdn",
        }
        if extra:
            fields.update(extra)
        return fields

    def build_payload(self, *, m_type: str = "B", s_type: str = "2") -> dict[str, str]:
        a = encode_a(self._base_fields(m_type=m_type, s_type=s_type))
        payload = {
            "sv": "SP",
            "ns": "CSTATUS",
            "ver": "1.1",
            "tm": str(int(time.time())),
            "ht": "pc",
            "cs": "html5",
            "uid": self.uid,
            "a": a,
        }
        payload["hash"] = compute_hash(payload)
        return payload

    def _next_s_type(self) -> str:
        self._tick += 1
        if self._tick % 6 == 0:
            return "L"
        return "3" if self._tick % 5 == 2 else "2"

    async def send(self, session: aiohttp.ClientSession) -> bool:
        s_type = self._next_s_type()
        if s_type == "L":
            payload = self.build_payload(m_type="L", s_type="")
        else:
            payload = self.build_payload(m_type="B", s_type=s_type)
        referer = f"{PLAY_ORIGIN}/{self.channel.user_id}/{self.channel.broad_no}"
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": PLAY_ORIGIN,
            "Referer": referer,
            "Accept": "*/*",
        }
        timeout = aiohttp.ClientTimeout(
            total=HTTP_TOTAL_TIMEOUT,
            connect=HTTP_CONNECT_TIMEOUT,
        )
        async with session.post(GATHER_URL, data=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            text = await resp.text()
            return "nRet" in text or text.strip() == "" or resp.status == 200
