"""B站 UID 核验（站点侧独立实现）。

与插件里的核验保持同一套规则与同一套"接口事实"：
- `x/web-interface/card` 不需要登录/签名，必须带 UA 与 Referer 否则容易被风控；
- **用户不存在时 HTTP 仍是 200，靠 `code == -404` 判断**；
- `-799` / `-352` / HTTP 412 属于风控，要与"不存在"区分开。

站点通过 urllib 同步调用（在 worker 线程里跑，不阻塞其它请求）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

CARD_API = "https://api.bilibili.com/x/web-interface/card"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
NOT_FOUND_CODES = {-400, -404, 62002}
CACHE_TTL = 600.0
CACHE_MAX = 5000  # 缓存条目上限，防止被大量不同 UID 刷爆内存
RETRY_DELAY = 1.0

_UID_IN_URL_RE = re.compile(r"(?:space\.bilibili\.com|bilibili\.com/space)/(\d{1,10})(?!\d)", re.I)
_UID_LABELED_RE = re.compile(r"(?:uid|Uid|UID)\s*[:：=]?\s*(\d{1,10})(?!\d)")
_UID_BARE_RE = re.compile(r"(?<!\d)(\d{2,10})(?!\d)")

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def extract_uid(text: str) -> str | None:
    """从文本里抽 UID：优先主页链接，其次 UID:123，最后裸数字（1 位数字不算）。"""
    raw = str(text or "")
    for pattern in (_UID_IN_URL_RE, _UID_LABELED_RE, _UID_BARE_RE):
        match = pattern.search(raw)
        if match:
            uid = match.group(1).lstrip("0")
            if uid.isdigit() and 1 <= len(uid) <= 10:
                return uid
    return None


def _fetch_json(url: str, timeout: float, referer: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": referer or "https://www.bilibili.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def lookup(uid: str, *, timeout: float = 10.0, retry_delay: float = RETRY_DELAY) -> tuple[dict[str, Any] | None, str, str]:
    """查询账号，返回（信息, 原因, 类型）。类型："" 成功 / not_found / error。"""
    uid = str(uid)
    cached = _cache.get(uid)
    now = time.time()
    if cached and now - cached[0] < CACHE_TTL:
        return dict(cached[1]), "", ""

    url = f"{CARD_API}?mid={uid}&photo=false&jsonp=jsonp"
    referer = f"https://space.bilibili.com/{uid}"
    payload: dict[str, Any] | None = None
    last_error = "查询失败"
    for attempt in range(2):
        try:
            payload = _fetch_json(url, timeout, referer)
        except urllib.error.HTTPError as exc:
            last_error = f"B站接口返回 HTTP {exc.code}"
            payload = None
        except Exception as exc:  # 网络/超时/JSON 解析
            last_error = f"请求失败：{exc}"
            payload = None
        if isinstance(payload, dict) and payload.get("code") == 0:
            break
        if isinstance(payload, dict) and payload.get("code") in NOT_FOUND_CODES:
            break
        if attempt == 0 and retry_delay:
            time.sleep(retry_delay)

    if not isinstance(payload, dict):
        return None, last_error, "error"
    code = payload.get("code")
    if code in NOT_FOUND_CODES:
        return None, "该 UID 在 B站不存在", "not_found"
    if code != 0:
        message = str(payload.get("message") or payload.get("msg") or code)
        return None, f"B站接口未返回数据（{message}）", "error"

    data = payload.get("data") or {}
    card = data.get("card") if isinstance(data.get("card"), dict) else {}
    level_info = card.get("level_info") if isinstance(card.get("level_info"), dict) else {}
    try:
        fans = int(card.get("fans") or data.get("follower") or 0)
    except (TypeError, ValueError):
        fans = 0
    try:
        level = int(level_info.get("current_level") or 0)
    except (TypeError, ValueError):
        level = 0
    info = {
        "uid": uid,
        "name": str(card.get("name") or ""),
        "level": level,
        "fans": fans,
        "sign": str(card.get("sign") or ""),
        "at": now,
    }
    if len(_cache) >= CACHE_MAX:
        # 简单清理：先删过期，还不够就丢掉最早写入的一半
        for key in [k for k, v in _cache.items() if now - v[0] >= CACHE_TTL]:
            _cache.pop(key, None)
        if len(_cache) >= CACHE_MAX:
            for key in sorted(_cache, key=lambda k: _cache[k][0])[: CACHE_MAX // 2]:
                _cache.pop(key, None)
    _cache[uid] = (now, dict(info))
    return info, "", ""


def clear_cache() -> None:
    _cache.clear()


def rule_check(
    info: dict[str, Any],
    *,
    min_level: int = 0,
    min_fans: int = 0,
    name_keywords: list[str] | None = None,
) -> tuple[bool, str]:
    """按规则校验账号信息，返回（是否通过, 原因）。"""
    if min_level and int(info.get("level") or 0) < int(min_level):
        return False, f"B站等级 {info.get('level')} 低于要求的 {min_level}"
    if min_fans and int(info.get("fans") or 0) < int(min_fans):
        return False, f"B站粉丝数 {info.get('fans')} 低于要求的 {min_fans}"
    keywords = [str(item) for item in (name_keywords or []) if str(item).strip()]
    if keywords:
        name = str(info.get("name") or "")
        if not any(keyword in name for keyword in keywords):
            return False, f"B站昵称「{name}」不含指定关键词（{'、'.join(keywords)}）"
    return True, ""


def verify(
    uid: str,
    *,
    rules: dict[str, Any],
    taken_by: str = "",
    timeout: float = 10.0,
    retry_delay: float = RETRY_DELAY,
) -> dict[str, Any]:
    """完整的站点侧核验。

    返回 `{"outcome": "approved|rejected|manual", "reason": str, "info": dict|None, "unverified": bool}`。
    `taken_by` 非空表示该 UID 已被别的 QQ 占用（pending/approved）。
    """
    info, error, kind = lookup(uid, timeout=timeout, retry_delay=retry_delay)
    if info is None:
        if kind == "not_found":
            # 账号确实不存在：无论配置如何都不放行
            return {"outcome": "rejected", "reason": error, "info": None, "unverified": False}
        on_error = str(rules.get("bili_on_error") or "manual").strip().lower()
        if on_error == "pass":
            return {"outcome": "approved", "reason": f"B站查询失败但按配置放行（{error}）", "info": None, "unverified": True}
        if on_error == "reject":
            return {"outcome": "rejected", "reason": error, "info": None, "unverified": False}
        return {"outcome": "manual", "reason": f"B站查询失败，需人工确认（{error}）", "info": None, "unverified": False}

    passed, reason = rule_check(
        info,
        min_level=int(rules.get("bili_min_level") or 0),
        min_fans=int(rules.get("bili_min_fans") or 0),
        name_keywords=list(rules.get("bili_name_keywords") or []),
    )
    if not passed:
        return {"outcome": "rejected", "reason": reason, "info": info, "unverified": False}
    if rules.get("bili_unique", True) and taken_by:
        return {
            "outcome": "rejected",
            "reason": f"该 UID 已被 QQ {taken_by} 使用过",
            "info": info,
            "unverified": False,
        }
    return {"outcome": "approved", "reason": "B站账号核验通过", "info": info, "unverified": False}
