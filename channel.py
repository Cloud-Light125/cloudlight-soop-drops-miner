from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import asyncio
import aiohttp

from .auth import cookie_header
from .constants import (
    DEFAULT_CHANNEL_BJID,
    DROPS_HASHTAG,
    DROPS_HASHTAG_SEARCH_REFERER,
    LIVE_API,
    PLAY_ORIGIN,
    SEARCH_API,
    USER_AGENT,
)
from .models import DropEvent, LiveChannel, Mission

LIVE_FORM = (
    "bid={bjid}&type=live&pwd=&player_type=html5&stream_type=common"
    "&quality=HD&mode=landing&from_api=0&is_revive=false"
)


@dataclass
class ChannelConfig:
    """直播间选择策略。"""

    mode: str = "smart"  # smart | manual | owesports
    manual_input: str = ""
    preferred_bjid: str = DEFAULT_CHANNEL_BJID
    priority_mission_id: str = "auto"  # auto 或 mission.drops_idx
    hang_without_missions: bool = True


PRIORITY_MISSION_AUTO = "auto"
ONE_STREAM_NOTICE = (
    "每个账号同一时间只能进入一个直播间。只有符合掉宝活动要求的直播间，才会累计对应任务的观看进度。"
)


def parse_stream_input(text: str) -> tuple[str, str | None]:
    """解析链接或 ID，返回 (bjid, broad_no|None)。"""
    text = text.strip()
    if not text:
        return "", None

    for pattern in (
        r"play\.sooplive\.com/([^/?#\s]+)(?:/(\d+))?",
        r"sooplive\.co\.kr/(?:live/)?([^/?#\s]+)(?:/(\d+))?",
        r"sooplive\.com/(?:live/)?([^/?#\s]+)(?:/(\d+))?",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1), match.group(2)

    if "/" in text:
        bjid, rest = text.split("/", 1)
        bjid = bjid.strip()
        rest = rest.strip()
        if bjid and rest.isdigit():
            return bjid, rest

    if text.isdigit():
        return "", text
    return text, None


async def fetch_live_channel(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    bjid: str,
    *,
    broad_no: str | None = None,
) -> LiveChannel | None:
    """通过 player_live_api 查询直播间是否在线。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": cookie_header(cookies),
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"{PLAY_ORIGIN}/{bjid}",
    }
    async with session.post(
        f"{LIVE_API}?bjid={bjid}",
        data=LIVE_FORM.format(bjid=bjid),
        headers=headers,
    ) as resp:
        data = await resp.json(content_type=None)
    channel: dict[str, Any] = data.get("CHANNEL") or {}
    if channel.get("RESULT") != 1:
        return None
    bno = str(channel.get("BNO") or broad_no or "")
    if not bno:
        return None
    nick = str(channel.get("BJ_NICK") or channel.get("BJID") or bjid)
    return LiveChannel(user_id=bjid, user_nick=nick, broad_no=bno, on_air=True)


def collect_mission_channel_candidates(missions: list[Mission]) -> list[LiveChannel]:
    """从 Drops 任务收集全部候选直播间（不依赖 onAir 字段）。"""
    seen: set[str] = set()
    result: list[LiveChannel] = []
    for mission in missions:
        for ch in mission.channels:
            if not ch.user_id or ch.user_id in seen:
                continue
            seen.add(ch.user_id)
            result.append(ch)
    return result


def is_category_fixed_mission(mission: Mission) -> bool:
    """固定型但无官方频道列表，需挂对应分类 #드롭스（如 이터널 리턴）。"""
    return (
        mission.is_fixed
        and not mission.channels
        and bool(mission.category_no or mission.category_name)
    )


def fixed_category_channels(
    mission: Mission,
    hashtag_live: list[LiveChannel],
) -> list[LiveChannel]:
    if not is_category_fixed_mission(mission):
        return []
    eligible = _lottery_eligible_channels(hashtag_live)
    return [ch for ch in eligible if channel_matches_mission_category(mission, ch)]


def has_fixed_category_live(
    missions: list[Mission],
    hashtag_live: list[LiveChannel],
) -> bool:
    for mission in missions:
        if not mission.is_event_active or not mission.is_fixed:
            continue
        if fixed_category_channels(mission, hashtag_live):
            return True
    return False


def fixed_official_ids(missions: list[Mission]) -> set[str]:
    """固定型任务关联的官方频道 ID。"""
    ids: set[str] = set()
    for mission in missions:
        if not mission.is_fixed:
            continue
        for ch in mission.channels:
            if ch.user_id:
                ids.add(ch.user_id)
    return ids


def has_active_fixed_missions(missions: list[Mission]) -> bool:
    return any(m.is_fixed and m.is_event_active and m.items for m in missions)


def has_active_lottery_missions(missions: list[Mission]) -> bool:
    return any(m.is_lottery and m.is_event_active and m.items for m in missions)


def ended_fixed_missions(missions: list[Mission]) -> list[Mission]:
    return [m for m in missions if m.is_fixed and m.is_event_ended and m.items]


def ended_lottery_missions(missions: list[Mission]) -> list[Mission]:
    return [m for m in missions if m.is_lottery and m.is_event_ended and m.items]


def _fixed_missions(missions: list[Mission]) -> list[Mission]:
    return [m for m in missions if m.is_fixed and m.items]


def fixed_column_summary(missions: list[Mission]) -> tuple[str, str]:
    """固定型列顶部常驻状态（中性文案，不含红色警告）。"""
    fixed = _fixed_missions(missions)
    if not fixed:
        return "暂无固定型任务", "#757575"
    active = [m for m in fixed if m.is_event_active]
    inactive = [m for m in fixed if m.is_event_ended]
    parts: list[str] = []
    if active:
        parts.append(f"进行中 {len(active)} 个")
    if inactive:
        not_open = sum(1 for m in inactive if m.is_not_yet_open)
        ended = sum(1 for m in inactive if m.is_truly_ended)
        if not_open:
            parts.append(f"未开放 {not_open} 个")
        if ended:
            parts.append(f"已结束 {ended} 个")
    color = "#2e7d32" if active else "#757575"
    return " · ".join(parts), color


def fixed_column_warning(
    missions: list[Mission],
    online_channels: list[LiveChannel],
    *,
    running_no_channel: bool = False,
) -> str:
    """固定型列顶单条红色提示，无则返回空字符串。"""
    fixed = _fixed_missions(missions)
    if not fixed:
        return ""

    active = [m for m in fixed if m.is_event_active]
    inactive = [m for m in fixed if m.is_event_ended]

    if not active:
        if any(m.is_not_yet_open for m in inactive):
            return "⚠ 当前未开放掉宝"
        if inactive:
            return "⚠ 固定型活动已结束，继续挂机不会累计该任务进度"
        return ""

    if running_no_channel or not is_fixed_progress_available(missions, online_channels):
        official_active = [m for m in active if m.channels]
        category_active = [m for m in active if is_category_fixed_mission(m)]
        if official_active and not category_active:
            return "⚠ 官方掉宝频道均未开播，固定型任务无法继续累计进度"
        if category_active:
            names = "、".join(
                dict.fromkeys(m.category_name or m.title[:12] for m in category_active)
            )
            return f"⚠ 「{names}」分类暂无 #드롭스 在线，固定型无法累计进度"
        return "⚠ 固定型任务暂无可用直播间"
    return ""


def is_fixed_progress_available(
    missions: list[Mission],
    online_channels: list[LiveChannel],
) -> bool:
    """固定型是否有官方频道在线（可累计进度）。"""
    if not has_active_fixed_missions(missions):
        inactive = [m for m in missions if m.is_fixed and m.is_event_ended and m.items]
        if inactive:
            return False
        return True
    online_ids = {ch.user_id for ch in online_channels}
    official = fixed_official_ids(missions)
    if official and online_ids & official:
        return True
    if has_fixed_category_live(missions, online_channels):
        return True
    for mission in missions:
        if not mission.is_fixed:
            continue
        for ch in mission.channels:
            if ch.on_air and ch.broad_no:
                return True
    return False


def normalize_category_no(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return value.strip()
    return str(int(digits))


def channel_matches_mission_category(mission: Mission, channel: LiveChannel) -> bool:
    """判断标签直播是否属于抽奖型任务对应分类。"""
    mission_no = normalize_category_no(mission.category_no)
    channel_no = normalize_category_no(channel.category_no)
    if mission_no and channel_no and mission_no == channel_no:
        return True
    mission_name = (mission.category_name or "").strip()
    if not mission_name:
        return False
    for name in channel.category_names:
        clean = name.strip()
        if not clean:
            continue
        if mission_name == clean or mission_name in clean or clean in mission_name:
            return True
    return False


def _lottery_eligible_channels(hashtag_live: list[LiveChannel]) -> list[LiveChannel]:
    return [ch for ch in hashtag_live if ch.has_drops]


def is_lottery_mission_live_available(
    mission: Mission,
    hashtag_live: list[LiveChannel],
) -> bool:
    """单个抽奖型任务是否有对应分类的 #드롭스 在线直播。"""
    eligible = _lottery_eligible_channels(hashtag_live)
    if not mission.category_no and not mission.category_name:
        return len(eligible) > 0
    return any(channel_matches_mission_category(mission, ch) for ch in eligible)


def lottery_missions_missing_live(
    missions: list[Mission],
    hashtag_live: list[LiveChannel],
) -> list[Mission]:
    """返回没有对应分类在线直播的抽奖型任务。"""
    missing: list[Mission] = []
    for mission in missions:
        if not mission.is_lottery or not mission.is_event_active or not mission.items:
            continue
        if not is_lottery_mission_live_available(mission, hashtag_live):
            missing.append(mission)
    return missing


def collect_mission_channels(missions: list[Mission]) -> list[LiveChannel]:
    """从进行中的 Drops 任务汇总标记为在线的直播间。"""
    seen: set[str] = set()
    result: list[LiveChannel] = []
    for mission in missions:
        if not mission.is_event_active:
            continue
        for ch in mission.online_channels():
            if ch.user_id in seen:
                continue
            seen.add(ch.user_id)
            result.append(ch)
    return result


def active_progress_missions(missions: list[Mission]) -> list[Mission]:
    """仍有未完成档位的进行中任务。"""
    result: list[Mission] = []
    for mission in missions:
        if not mission.is_event_active or not mission.items:
            continue
        item = mission.active_item()
        if item is None or item.mission_success or item.view_time >= item.give_term:
            continue
        result.append(mission)
    return result


def mission_progresses_on_channel(mission: Mission, channel: LiveChannel | None) -> bool:
    """当前直播间是否能为该任务累计进度。"""
    if channel is None or not mission.is_event_active:
        return False
    if mission.is_fixed:
        official = fixed_official_ids([mission])
        if official:
            return channel.user_id in official
        if mission.category_no or mission.category_name:
            return channel_matches_mission_category(mission, channel)
        return any(ch.user_id == channel.user_id for ch in mission.channels if ch.user_id)
    if mission.is_lottery:
        return channel_matches_mission_category(mission, channel)
    if mission.is_random:
        if any(ch.user_id == channel.user_id for ch in mission.channels if ch.user_id):
            return True
        return channel_matches_mission_category(mission, channel)
    return False


def channel_drops_type_tags(
    channel: LiveChannel,
    missions: list[Mission],
    events: list[DropEvent] | None = None,
) -> list[str]:
    """推断直播间对应的掉宝类型标签（固定/抽奖/随机）。"""
    tags: list[str] = []
    seen: set[str] = set()
    for mission in missions:
        if not mission.is_event_active:
            continue
        matched = mission_progresses_on_channel(mission, channel)
        if not matched and mission.is_lottery:
            matched = channel_matches_mission_category(mission, channel)
        if matched:
            short = mission.type_short
            if short not in seen:
                seen.add(short)
                tags.append(short)
        elif mission.is_fixed and (mission.category_no or mission.category_name):
            if channel_matches_mission_category(mission, channel):
                if mission.type_short not in seen:
                    seen.add(mission.type_short)
                    tags.append(mission.type_short)
    for event in events or []:
        if not event.is_random or not event.live:
            continue
        if event.matches_channel(channel):
            if "随机" not in seen:
                seen.add("随机")
                tags.append("随机")
    return tags


def format_channel_drops_label(
    channel: LiveChannel,
    missions: list[Mission],
    events: list[DropEvent] | None = None,
) -> str:
    tags = channel_drops_type_tags(channel, missions, events)
    if not tags:
        return "掉宝"
    return "/".join(tags)


def missions_for_channel(missions: list[Mission], channel: LiveChannel | None) -> list[Mission]:
    return [m for m in active_progress_missions(missions) if mission_progresses_on_channel(m, channel)]


def mission_pick_label(mission: Mission) -> str:
    item = mission.active_item()
    progress = f"{item.view_time}/{item.give_term}" if item else "-"
    title = mission.title if len(mission.title) <= 30 else mission.title[:27] + "…"
    return f"[{mission.type_label}] {title} ({progress})"


def filter_missions_by_priority(
    missions: list[Mission],
    priority_mission_id: str,
) -> list[Mission]:
    active = active_progress_missions(missions)
    if not priority_mission_id or priority_mission_id == PRIORITY_MISSION_AUTO:
        return active
    selected = [m for m in active if m.drops_idx == priority_mission_id]
    # A manually selected campaign may end between two polls. Continue with
    # the current active missions instead of pinning the miner to a stale ID
    # forever; the persisted setting is still left untouched.
    return selected or active


def manual_channel_mismatch_warnings(
    channel: LiveChannel,
    missions: list[Mission],
) -> list[str]:
    """手动选台且分类不匹配时的警告文案（仅提示，不阻止挂机）。"""
    targets = active_progress_missions(missions)
    if not targets:
        return []

    served = missions_for_channel(missions, channel)
    served_ids = {m.drops_idx for m in served}
    warnings: list[str] = []
    cate = channel.category_names[0] if channel.category_names else "未知分类"
    for mission in targets:
        if mission.drops_idx in served_ids:
            continue
        need = mission.category_name or mission.category_no or "官方/指定频道"
        title = mission.title if len(mission.title) <= 24 else mission.title[:21] + "…"
        warnings.append(
            f"手动选台：当前直播间分类为「{cate}」，与任务「{title}」所需「{need}」不一致，"
            f"该任务进度可能不会增加。"
        )
    if warnings:
        warnings.append(ONE_STREAM_NOTICE)
    return warnings


def format_channel_preview(channel: LiveChannel | None) -> str:
    if channel is None:
        return "暂无可进入的直播间（请刷新列表或等待开播）"
    cate = f" · {channel.category_names[0]}" if channel.category_names else ""
    bno = f" · {channel.broad_no}" if channel.broad_no else ""
    nick = channel.user_nick or channel.user_id
    return f"预计进入：{nick} ({channel.user_id}){cate}{bno}"


async def _pick_mission_channel(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    mission: Mission,
    *,
    hashtag_live: list[LiveChannel],
    eligible: list[LiveChannel],
) -> LiveChannel | None:
    if mission.is_fixed:
        for och in mission.channels:
            if not och.user_id:
                continue
            try:
                live = await fetch_live_channel(
                    session, cookies, och.user_id, broad_no=och.broad_no
                )
            except Exception:
                live = None
            if live:
                return live
        matched = fixed_category_channels(mission, hashtag_live)
        if matched:
            return matched[0]
        return None

    if mission.is_lottery:
        matched = [ch for ch in eligible if channel_matches_mission_category(mission, ch)]
        if matched:
            return matched[0]
        if eligible and not mission.category_no and not mission.category_name:
            return eligible[0]
        return None

    if mission.is_random:
        for och in mission.channels:
            if not och.user_id:
                continue
            try:
                live = await fetch_live_channel(
                    session, cookies, och.user_id, broad_no=och.broad_no
                )
            except Exception:
                live = None
            if live:
                return live
        for ch in eligible:
            if mission_progresses_on_channel(mission, ch):
                return ch
    return None


async def _pick_for_active_missions(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    missions: list[Mission],
    *,
    hashtag_live: list[LiveChannel] | None = None,
    priority_mission_id: str = PRIORITY_MISSION_AUTO,
) -> LiveChannel | None:
    """智能选台：按优先任务匹配可累计进度的直播间。"""
    active = filter_missions_by_priority(missions, priority_mission_id)
    if not active:
        return None

    if hashtag_live is None:
        hashtag_live = await fetch_hashtag_drops_channels(session, cookies)

    eligible = _lottery_eligible_channels(hashtag_live)

    if priority_mission_id != PRIORITY_MISSION_AUTO:
        return await _pick_mission_channel(
            session, cookies, active[0], hashtag_live=hashtag_live, eligible=eligible
        )

    for mission in [m for m in active if m.is_fixed]:
        ch = await _pick_mission_channel(
            session, cookies, mission, hashtag_live=hashtag_live, eligible=eligible
        )
        if ch:
            return ch
    for mission in [m for m in active if m.is_lottery]:
        ch = await _pick_mission_channel(
            session, cookies, mission, hashtag_live=hashtag_live, eligible=eligible
        )
        if ch:
            return ch
    for mission in [m for m in active if m.is_random]:
        ch = await _pick_mission_channel(
            session, cookies, mission, hashtag_live=hashtag_live, eligible=eligible
        )
        if ch:
            return ch
    return None


async def _pick_manual_channel(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    config: ChannelConfig,
) -> LiveChannel | None:
    """手动选台：用户指定优先，不走任务智能匹配。"""
    if config.manual_input.strip():
        bjid, bno = parse_stream_input(config.manual_input)
        if bjid:
            return await fetch_live_channel(session, cookies, bjid, broad_no=bno)
        if bno:
            online, _ = await fetch_live_drops_channels(session, cookies, [])
            for candidate in online:
                if candidate.broad_no == bno:
                    return candidate
    online, _ = await fetch_live_drops_channels(session, cookies, [])
    return online[0] if online else None


async def _pick_owesports_channel(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    config: ChannelConfig,
) -> LiveChannel | None:
    """仅挂 owesports，未开播则返回 None（等待）。"""
    return await fetch_live_channel(session, cookies, config.preferred_bjid)


def _search_headers(cookies: dict[str, str]) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Cookie": cookie_header(cookies),
        "Accept": "application/json, */*",
        "Referer": DROPS_HASHTAG_SEARCH_REFERER,
    }


async def _fetch_search_session_key(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
) -> str:
    """获取搜索 API 会话 key（与官网 #드롭스 搜索页相同流程）。"""
    params = {
        "l": "DF",
        "m": "stopWord",
        "v": 2,
        "w": "webk",
        "ut": "sv",
        "d": DROPS_HASHTAG,
        "acttype": "live",
        "stype": "hash",
        "location": "drops",
        "isHashSearch": 1,
        "tagname": DROPS_HASHTAG,
    }
    async with session.get(
        SEARCH_API, params=params, headers=_search_headers(cookies)
    ) as resp:
        data = await resp.json(content_type=None)
    return str(data.get("sessionKey") or "")


def _live_channel_from_search(item: dict[str, Any]) -> LiveChannel | None:
    user_id = str(item.get("user_id") or "").strip()
    broad_no = str(item.get("broad_no") or "").strip()
    if not user_id or not broad_no:
        return None
    nick = str(item.get("user_nick") or item.get("station_name") or user_id)
    names: list[str] = []
    cate_name = str(item.get("broad_cate_name") or "").strip()
    if cate_name:
        names.append(cate_name)
    for tag in item.get("category_tags") or []:
        if isinstance(tag, str) and tag.strip():
            names.append(tag.strip())
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            deduped.append(name)
    return LiveChannel(
        user_id=user_id,
        user_nick=nick,
        broad_no=broad_no,
        on_air=True,
        category_no=str(item.get("broad_cate_no") or "").strip() or None,
        category_names=deduped,
        has_drops=str(item.get("is_drops", "1")) == "1",
    )


async def fetch_hashtag_drops_channels(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    *,
    page_size: int = 30,
    max_pages: int = 3,
) -> list[LiveChannel]:
    """从官网 #드롭스 标签直播搜索拉取在线直播间。"""
    session_key = await _fetch_search_session_key(session, cookies)
    if not session_key:
        return []

    seen: set[str] = set()
    result: list[LiveChannel] = []
    for page in range(1, max_pages + 1):
        params = {
            "l": "DF",
            "m": "liveSearch",
            "c": "UTF-8",
            "w": "webk",
            "isMobile": 0,
            "onlyParent": 1,
            "szType": "json",
            "sck_session_key": session_key,
            "szKeyword": DROPS_HASHTAG,
            "nPageNo": page,
            "nListCnt": page_size,
            "tab": "LIVE",
            "location": "drops",
            "isHashSearch": 1,
            "v": "2.0",
        }
        async with session.get(
            SEARCH_API, params=params, headers=_search_headers(cookies)
        ) as resp:
            data = await resp.json(content_type=None)
        broads: list[dict[str, Any]] = data.get("REAL_BROAD") or data.get("realBroad") or []
        for item in broads:
            ch = _live_channel_from_search(item)
            if ch and ch.user_id not in seen:
                seen.add(ch.user_id)
                result.append(ch)
        if not data.get("HAS_MORE_LIST") or not broads:
            break
    return result


async def fetch_live_drops_channels(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    missions: list[Mission],
) -> tuple[list[LiveChannel], list[LiveChannel]]:
    """拉取可掉宝直播间：优先 #드롭스 标签搜索，并补充任务官方频道。"""
    mission_candidates = collect_mission_channel_candidates(missions)
    hashtag_live = await fetch_hashtag_drops_channels(session, cookies)

    online_ids = {ch.user_id for ch in hashtag_live}
    extra_live: list[LiveChannel] = []

    async def probe_mission(ch: LiveChannel) -> LiveChannel | None:
        if ch.user_id in online_ids:
            return None
        try:
            return await fetch_live_channel(session, cookies, ch.user_id, broad_no=ch.broad_no)
        except Exception:
            return None

    if mission_candidates:
        probed = await asyncio.gather(*(probe_mission(ch) for ch in mission_candidates))
        for ch in probed:
            if ch is not None:
                extra_live.append(ch)
                online_ids.add(ch.user_id)

    online = hashtag_live + extra_live
    offline_candidates = [ch for ch in mission_candidates if ch.user_id not in online_ids]
    return online, offline_candidates


async def pick_channel(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    missions: list[Mission],
    config: ChannelConfig,
) -> LiveChannel | None:
    """按策略选台：智能 / 手动 / 仅 owesports 互斥。"""
    if config.mode == "manual":
        return await _pick_manual_channel(session, cookies, config)

    if config.mode == "owesports":
        return await _pick_owesports_channel(session, cookies, config)

    # smart
    mission_pick = await _pick_for_active_missions(
        session,
        cookies,
        missions,
        priority_mission_id=config.priority_mission_id,
    )
    if mission_pick is not None:
        return mission_pick

    if config.hang_without_missions:
        online, _ = await fetch_live_drops_channels(session, cookies, missions)
        return online[0] if online else None
    return None
