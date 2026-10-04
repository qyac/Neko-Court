"""临时审核群管理插件。

功能：
1. 每天固定时间（cleanup_time）把审核群里的普通成员全部移出；
2. 新人入群时自动发送一道审核问题；
3. 接收并判定成员的回答：答错 ``max_attempts`` 次踢出，答对下发当日验证码；
4. 每天 ``code_reset_time`` 重置一次随机验证码，管理员可用指令查询。

另外提供了一个 Dashboard 插件 Pages 设置页（``pages/settings``），可以把全部配置项分组列出并直接修改保存；
后端接口为 ``GET/POST /astrbot_plugin_temp_review_group/settings``。

设计约定（遵循 AstrBot 插件开发规范）：
- 只使用 AstrBot 公开 API，无第三方依赖，网络调用全部异步；
- 持久化数据写入 ``data/plugin_data/astrbot_plugin_temp_review_group/``，不写入插件目录；
- 插件加载失败要“响亮”：配置错误在加载时写日志，且不会静默踢人；
- 可调参数一律走 ``_conf_schema.json``，代码中不硬编码；
- WebUI 保存的配置项同样按 schema 校验，非法值拒绝写入而不是静默回退。

平台限制：踢人、拉取群成员列表依赖 OneBot v11（aiocqhttp）接口。
"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import json
import random
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools

try:  # 不同版本对消息链的导出位置略有差异
    from astrbot.api.event import MessageChain
except Exception:  # pragma: no cover - 兼容旧版本
    from astrbot.core.message.message_event_result import MessageChain

try:  # 插件 Pages 的后端 API 只在较新的 AstrBot 中提供
    from astrbot.api.web import error_response, json_response, request

    WEB_API_AVAILABLE = True
except Exception:  # pragma: no cover - 兼容没有 astrbot.api.web 的版本
    error_response = json_response = request = None  # type: ignore[assignment]
    WEB_API_AVAILABLE = False

PLUGIN_NAME = "astrbot_plugin_temp_review_group"
# 下面两项与 metadata.yaml 保持一致，仅在读不到插件元数据时作为兜底展示
PLUGIN_DISPLAY_NAME = "临时审核群管理"
PLUGIN_VERSION = "v1.1.1"
STATE_FILE = "state.json"
SCHEMA_FILE = "_conf_schema.json"
WEB_API_PREFIX = f"/{PLUGIN_NAME}"
# 时间配置里表示"关闭"的写法
DISABLED_TIME_WORDS = {"off", "none", "disable", "disabled", "-", "关闭"}
# 这些配置项需要语义校验，避免写进去后运行时才静默回退
TIME_CONFIG_KEYS = {"cleanup_time", "code_reset_time"}

# unified_msg_origin 的格式为 "平台ID:消息类型:会话ID"
GROUP_MESSAGE_TYPE = "GroupMessage"
FRIEND_MESSAGE_TYPE = "FriendMessage"
try:  # 优先使用枚举值，避免手写字符串出错
    from astrbot.api.platform import MessageType as _MessageType

    GROUP_MESSAGE_TYPE = _MessageType.GROUP_MESSAGE.value
    FRIEND_MESSAGE_TYPE = _MessageType.FRIEND_MESSAGE.value
except Exception:  # pragma: no cover - 兼容旧版本
    pass

# 归一化文本时需要剔除的标点与空白（全角会被 NFKC 先转成半角）
_PUNCTUATION = set(
    " \t\r\n\u3000!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    "，。、；：？！“”‘’（）【】《》〈〉…—～·￥"
)
_DIGITS_RE = re.compile(r"\d{5,12}")
# 命中这些过滤器说明这条消息是给某个指令的，不应被当成审核回答
_COMMAND_FILTER_NAMES = {"CommandFilter", "CommandGroupFilter", "RegexFilter"}

# 验证码发送方式
CODE_SEND_MODES = ("private", "group", "both")
# 私聊通道：auto = 先试群临时会话（无需加好友）再退回好友私聊
PRIVATE_SEND_CHANNELS = ("auto", "temp_session", "friend")
# 答案匹配方式：inherit 只用于单道题的覆盖设置（表示跟随全局）
MATCH_MODES = ("contains", "exact", "regex", "fuzzy")
MATCH_MODE_CHOICES = ("inherit",) + MATCH_MODES
# LLM 审核：系统提示词固定，要求模型只回一个判定词
LLM_JUDGE_SYSTEM_PROMPT = (
    "你是入群审核判定器。根据审核问题、参考答案和申请人的回答，判断申请人是否通过。"
    "只输出 PASS 或 FAIL 一个词，不要解释、不要标点、不要多余内容。"
)
_LLM_PASS_TOKENS = {"pass", "yes", "true", "ok", "通过", "合格", "是"}
_LLM_FAIL_TOKENS = {"fail", "no", "false", "不通过", "未通过", "不合格", "否"}
# 退化的全文扫描：必须先看否定词，否则「不通过」会被「通过」误判为通过
_LLM_FAIL_SCAN = ("fail", "不通过", "未通过", "不予通过", "不合格", "拒绝", "不满足", "答非所问")
_LLM_PASS_SCAN = ("pass", "通过", "合格", "满足要求", "允许入群")
_JUDGE_SOURCE_LABELS = {"rule": "规则", "llm": "大模型", "rule-fallback": "规则（模型不可用回退）"}

# 题库「LLM 自动填入」：把一段文本解析成题目与答案
QUESTION_BANK_TEMPLATE_KEY = "question_item"
QUESTION_PARSE_MAX_CHARS = 8000
QUESTION_PARSE_MAX_ITEMS = 30
LLM_PARSE_SYSTEM_PROMPT = (
    "你是题库整理助手。用户会给你一段文本（公告、文档、聊天记录等），"
    "你要从中提取入群审核问题与可接受的答案，并只输出 JSON。"
)
LLM_PARSE_PROMPT = (
    "从下面的文本里整理一份入群审核题库。要求：\n"
    "1) 只输出一个 JSON 数组，不要解释、不要 markdown 代码块；\n"
    '2) 每个元素形如 {"question": "问题", "answers": ["答案1", "答案2"], "hint": "", "match_mode": "inherit"}；\n'
    "3) answers 要尽量覆盖文本里出现的等价说法（同义词、简称、别名、大小写变体），每条尽量简短；\n"
    "4) 文本里没有明确答案时 answers 可以是空数组，但 question 必须保留；\n"
    "5) 最多 {limit} 条，按文本中出现的顺序输出，不要编造文本里没有的信息。\n\n"
    "文本如下：\n{text}"
)


def _parse_llm_verdict(raw: str) -> bool | None:
    """把模型回复解析为 True/False；无法判定时返回 None（调用方回退规则判定）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    head = re.split(r"[\s,，。.!！?？:：;；、\-—]+", text, maxsplit=1)[0].strip().lower()
    if head in _LLM_FAIL_TOKENS:
        return False
    if head in _LLM_PASS_TOKENS:
        return True
    lowered = text.lower()
    for word in _LLM_FAIL_SCAN:
        if word in lowered:
            return False
    for word in _LLM_PASS_SCAN:
        if word in lowered:
            return True
    return None

HELP_TEXT = """📖 临时审核群管理

审核流程（自动）：新人入群 → 机器人提问 → 成员回答 → 答对下发验证码 / 答错 N 次移出。
判定方式由 review_mode 决定（规则 / 大模型 / 两者结合）；验证码默认私聊发送（code_send_mode）。

管理指令（需管理员权限）：
· /审核码           查询当前验证码（别名：/验证码）
· /设定审核码 <码>  手动设定验证码（别名：/设定验证码）
· /审核 状态        查看验证码、待审核成员、已通过人数
· /审核 题库        查看问题库/答案库（含抽中与通过统计）
· /审核 试答 [#题号] <回答>  先验证这句话会不会通过，再调答案库
· /审核 诊断        排查「新人入群没收到消息」：事件计数、配置、发送自检
· /审核 重置码      立即重新生成验证码
· /审核 设定码 <码> 手动设定验证码
· /审核 放行 <QQ> [群号]   手动放行并下发验证码
· /审核 补发 <QQ> [群号]   给成员补发当日验证码（私聊）
· /审核 重审 <QQ> [群号]   清空记录并重新提问
· /审核 踢出 <QQ> [群号]   手动移出成员
· /审核 清理 [群号]        立即执行每日清理（留空=所有审核群）
· /审核 帮助        显示本帮助

说明：不填群号时默认使用当前群（若当前群是审核群）或第一个审核群。"""


def _normalize_text(text: str) -> str:
    """全角转半角、转小写、去掉空白与标点，用于宽松比对答案。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in text if ch not in _PUNCTUATION)


def _truthy(value: Any) -> bool:
    """宽松地把配置值当布尔看（配置里可能是 true/"true"/1）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "开启"}


def _raw_dict(event: AstrMessageEvent) -> dict:
    """取出平台原始事件（aiocqhttp 中是一个 dict 子类）。"""
    raw = getattr(event.message_obj, "raw_message", None)
    if isinstance(raw, dict):
        return raw
    try:
        return dict(raw)  # type: ignore[arg-type]
    except Exception:
        return {}


def _has_command_handler(event: AstrMessageEvent) -> bool:
    """判断本次事件是否命中了某个指令（含其它插件的指令/正则监听）。

    命中指令时，审核回答判定必须让路，否则 ``/审核码`` 之类的指令会被当成答题内容。
    """
    handlers = event.get_extra("activated_handlers") or []
    for handler in handlers:
        for handler_filter in getattr(handler, "event_filters", None) or []:
            if type(handler_filter).__name__ in _COMMAND_FILTER_NAMES:
                return True
    return False


def _as_number(value: str) -> int | str:
    """OneBot 接口通常要求数字型 ID，非纯数字时原样传递。"""
    text = str(value)
    return int(text) if text.isdigit() else text


class TempReviewGroup(Star):
    """临时审核群管理插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config: AstrBotConfig = config

        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._loops: list[asyncio.Task] = []
        self._jobs: set[asyncio.Task] = set()

        self._ready = False
        self._state: dict[str, Any] = self._empty_state()
        self._state_path: Path | None = None

        self._tz_name = ""
        self._tz_value: tzinfo | None = None
        self._role_cache: dict[str, tuple[float, str]] = {}
        self._login_ids: dict[str, str] = {}
        self._schema_cache: dict[str, Any] | None = None
        self._group_code_notice_logged = False
        self._private_leak_warned = False
        # 运行期诊断信息（不落盘，重载后清零）：用于回答"为什么没给新人发消息"
        self._diag: dict[str, Any] = {
            "counters": {},
            "last_notice": None,
            "last_message": None,
            "last_question": None,
            "last_reason": "",
        }

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """插件被激活时调用。"""
        self._ensure_ready()
        self._register_web_apis()
        self._warn_config()
        logger.info(
            "[审核群] 插件已加载，审核群：%s",
            "、".join(self._review_groups()) or "（未配置）",
        )

    async def terminate(self) -> None:
        """插件被禁用或重载时调用，必须清理后台任务。"""
        self._stop.set()
        tasks = [*self._loops, *self._jobs]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[审核群] 后台任务退出时出现异常")
        self._loops.clear()
        self._jobs.clear()
        self._save_state()

    # ------------------------------------------------------------------ 初始化辅助

    def _ensure_ready(self) -> None:
        """幂等初始化：加载状态、保证验证码存在、启动后台循环。"""
        if self._ready:
            return
        self._ready = True
        self._prepare_state_path()
        self._state = self._load_state()
        if not self._state.get("code"):
            self._state["code"] = self._generate_code()
            self._state["code_date"] = self._today_str()
            self._state["code_created_at"] = time.time()
            self._save_state()
            logger.info("[审核群] 已生成初始验证码（可用 /审核码 查询）。")
        self._ensure_loops()

    def _prepare_state_path(self) -> None:
        directory: Path | None = None
        try:
            directory = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as exc:
            logger.warning("[审核群] 无法通过 StarTools 获取数据目录（%s），改用 data/plugin_data。", exc)
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                directory = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
                directory.mkdir(parents=True, exist_ok=True)
            except Exception as fallback_exc:
                logger.error("[审核群] 无法创建插件数据目录：%s", fallback_exc)
                directory = None
        self._state_path = (directory / STATE_FILE) if directory else None

    def _warn_config(self) -> None:
        if not self._review_groups():
            logger.warning("[审核群] 未配置 review_groups（审核群群号），插件不会执行任何审核或清理动作。")
        if not self._admin_ids():
            logger.warning("[审核群] 未配置 admin_ids，管理指令仅对 AstrBot 全局管理员开放。")
        if self._pick_question() is None:
            logger.error("[审核群] questions 为空或缺少 answers，无法发起审核，请在插件配置中补充。")
        if self._review_mode() != "rule":
            if not self._provider_options():
                logger.error(
                    "[审核群] review_mode=%s 但当前没有任何可用的对话模型 Provider，"
                    "审核会一直回退到规则判定。",
                    self._review_mode(),
                )
            elif not str(self._get("review_llm_provider", "") or "").strip():
                logger.warning(
                    "[审核群] review_mode=%s 且未指定 review_llm_provider，将跟随各会话当前模型。",
                    self._review_mode(),
                )
        if self._code_send_mode() != "group" and not self._bool("code_fallback_to_group", False):
            logger.info("[审核群] 验证码按 %s 发送；私聊失败时不会回退到群内（可用 /审核 补发）。", self._code_send_mode())
        # 主动校验一次时间配置，尽早暴露写错的取值
        self._time_str("cleanup_time", "00:00")
        self._time_str("code_reset_time", "00:00")

    def _ensure_loops(self) -> None:
        if self._loops or self._stop.is_set():
            return
        self._loops.append(asyncio.create_task(self._code_loop(), name="temp-review-code"))
        self._loops.append(asyncio.create_task(self._cleanup_loop(), name="temp-review-cleanup"))

    def _spawn(self, coro: Any) -> None:
        """启动一次性后台任务（例如手动清理），并纳入 terminate 的统一清理。"""
        task = asyncio.create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(self._job_done)

    def _job_done(self, task: asyncio.Task) -> None:
        self._jobs.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("[审核群] 后台任务执行失败：%s", error, exc_info=error)

    # ------------------------------------------------------------------ 配置读取

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "code": "",
            "code_date": "",
            "code_created_at": 0.0,
            "last_cleanup_date": "",
            "pending": {},
            "approved": {},
            "question_stats": {},
            "group_platform": {},
            "self_ids": [],
        }

    def _get(self, key: str, default: Any) -> Any:
        if not isinstance(self.config, dict):
            return default
        value = self.config.get(key, default)
        return default if value is None else value

    def _bool(self, key: str, default: bool) -> bool:
        value = self._get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "开启"}

    def _int(self, key: str, default: int, minimum: int | None = None) -> int:
        try:
            value = int(self._get(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    def _float(self, key: str, default: float, minimum: float | None = None) -> float:
        try:
            value = float(self._get(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    def _template(self, key: str) -> str:
        return str(self._get(key, "") or "")

    def _id_list(self, key: str) -> list[str]:
        raw = self._get(key, [])
        if isinstance(raw, str):
            raw = re.split(r"[\s,，;；|]+", raw)
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        for item in raw:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _review_groups(self) -> list[str]:
        return self._id_list("review_groups")

    def _admin_ids(self) -> list[str]:
        return self._id_list("admin_ids")

    def _exempt_ids(self) -> list[str]:
        return self._id_list("exempt_user_ids")

    def _max_attempts(self) -> int:
        return self._int("max_attempts", 3, minimum=1)

    def _questions(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        """问题库。每道题：enabled/question/hint/answers/match_mode（inherit=跟随全局）。"""
        raw = self._get("questions", [])
        if not isinstance(raw, list):
            return []
        questions: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            answers = item.get("answers")
            if isinstance(answers, str):
                answers = re.split(r"[\n,，;；|]+", answers)
            if not isinstance(answers, list):
                answers = []
            mode = str(item.get("match_mode") or "inherit").strip().lower()
            entry = {
                "enabled": _truthy(item.get("enabled", True)),
                "question": str(item.get("question") or "").strip(),
                "hint": str(item.get("hint") or "").strip(),
                "answers": [str(answer).strip() for answer in answers if str(answer).strip()],
                "match_mode": mode if mode in MATCH_MODE_CHOICES else "inherit",
            }
            if not include_disabled and not entry["enabled"]:
                continue
            questions.append(entry)
        return questions

    def _common_answers(self) -> list[str]:
        """通用答案库：任何题目下命中都直接通过（邀请码/口令）。"""
        raw = self._get("common_answers", [])
        if isinstance(raw, str):
            raw = re.split(r"[\n,，;；|]+", raw)
        if not isinstance(raw, list):
            return []
        answers: list[str] = []
        for item in raw:
            text = str(item).strip()
            if text and text not in answers:
                answers.append(text)
        return answers

    def _match_mode(self) -> str:
        mode = str(self._get("match_mode", "contains") or "contains").strip().lower()
        return mode if mode in MATCH_MODES else "contains"

    def _resolve_match_mode(self, mode: str) -> str:
        """把单题的 match_mode（可能是 inherit）解析成实际使用的模式。"""
        resolved = str(mode or "inherit").strip().lower()
        return resolved if resolved in MATCH_MODES else self._match_mode()

    def _fuzzy_threshold(self) -> float:
        return min(1.0, self._float("fuzzy_threshold", 0.8, minimum=0.5))

    def _question_usable(self, entry: dict[str, Any]) -> bool:
        """题目可用 = 有题干，且（该题有答案 或 配了通用答案库）。"""
        return bool(entry.get("question")) and bool(entry.get("answers") or self._common_answers())

    def _pick_question(self) -> dict[str, Any] | None:
        """从已启用且可用的题目里随机抽一条。"""
        usable = [entry for entry in self._questions() if self._question_usable(entry)]
        return random.choice(usable) if usable else None

    def _generate_code(self) -> str:
        charset = str(self._get("code_charset", "") or "ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
        charset = "".join(dict.fromkeys(charset))  # 去重，避免概率被污染
        if len(charset) < 4:
            logger.warning("[审核群] code_charset 可用字符少于 4 个，已回退为默认字符集。")
            charset = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        length = self._int("code_length", 6, minimum=4)
        length = min(length, 32)
        code = "".join(secrets.choice(charset) for _ in range(length))
        # 至少包含一个字母和一个数字，避免出现纯数字/纯字母的极端情况
        if not any(ch.isdigit() for ch in code) or not any(ch.isalpha() for ch in code):
            code = code[:-2] + secrets.choice("23456789") + secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ")
        return code

    # ------------------------------------------------------------------ 时间相关

    def _tz(self) -> tzinfo | None:
        name = str(self._get("timezone", "") or "").strip()
        if not name:
            return None
        if name == self._tz_name:
            return self._tz_value
        self._tz_name = name
        try:
            from zoneinfo import ZoneInfo

            self._tz_value = ZoneInfo(name)
        except Exception as exc:
            logger.warning("[审核群] 时区 %s 不可用（%s），改用系统本地时区。", name, exc)
            self._tz_value = None
        return self._tz_value

    def _now(self) -> datetime:
        return datetime.now(self._tz())

    def _today_str(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _at_time(self, now: datetime, hhmm: str) -> datetime:
        hour, minute = (int(part) for part in hhmm.split(":"))
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _time_str(self, key: str, default: str) -> str | None:
        """解析 HH:MM；返回 None 表示该项被显式关闭。"""
        raw = str(self._get(key, default) or "").strip()
        if not raw or raw.lower() in DISABLED_TIME_WORDS:
            return None
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]?\d", raw):
            logger.warning("[审核群] 配置 %s=%r 不是合法的 HH:MM，已回退为 %s。", key, raw, default)
            return default
        hour, minute = raw.split(":")
        return f"{int(hour):02d}:{int(minute):02d}"

    def _next_time_at(self, key: str, default: str) -> datetime | None:
        """计算某个 HH:MM 配置项的下一次触发时间；关闭时返回 None。"""
        hhmm = self._time_str(key, default)
        if not hhmm:
            return None
        now = self._now()
        target = self._at_time(now, hhmm)
        return target if now < target else target + timedelta(days=1)

    def _next_reset_at(self) -> datetime | None:
        return self._next_time_at("code_reset_time", "00:00")

    def _expire_text(self) -> str:
        target = self._next_reset_at()
        if target is None:
            return "管理员下次重置前"
        return target.strftime("%m-%d %H:%M")

    def _fmt_ts(self, timestamp: Any) -> str:
        try:
            value = float(timestamp)
        except (TypeError, ValueError):
            return "未知"
        if value <= 0:
            return "未知"
        return datetime.fromtimestamp(value, self._tz()).strftime("%m-%d %H:%M")

    @staticmethod
    def _humanize(seconds: float) -> str:
        seconds = max(0, int(seconds))
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        if days:
            return f"{days} 天 {hours} 小时"
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分"
        return f"{seconds} 秒"

    async def _sleep(self, seconds: float) -> None:
        """可被 terminate 提前唤醒的睡眠。"""
        deadline = time.monotonic() + max(0.0, float(seconds))
        while not self._stop.is_set():
            remain = deadline - time.monotonic()
            if remain <= 0:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=remain)
                return
            except asyncio.TimeoutError:
                return

    # ------------------------------------------------------------------ 后台循环

    async def _code_loop(self) -> None:
        """每天 code_reset_time 轮换验证码；机器人停机期间的轮换会在启动后补做。"""
        await self._sleep(3)
        while not self._stop.is_set():
            try:
                hhmm = self._time_str("code_reset_time", "00:00")
                if not hhmm:
                    await self._sleep(3600)
                    continue
                now = self._now()
                target = self._at_time(now, hhmm)
                if now >= target:
                    if str(self._state.get("code_date") or "") != now.strftime("%Y-%m-%d"):
                        await self._rotate_code("定时重置")
                    target += timedelta(days=1)
                delay = (target - self._now()).total_seconds()
                await self._sleep(min(max(delay, 1.0), 60.0))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[审核群] 验证码重置循环出现异常")
                await self._sleep(60)

    async def _cleanup_loop(self) -> None:
        """每天 cleanup_time 清理审核群；停机期间错过的清理会在启动后补做一次。"""
        await self._sleep(15)
        while not self._stop.is_set():
            try:
                hhmm = self._time_str("cleanup_time", "00:00")
                if not hhmm:
                    await self._sleep(3600)
                    continue
                now = self._now()
                target = self._at_time(now, hhmm)
                if now >= target:
                    today = now.strftime("%Y-%m-%d")
                    if str(self._state.get("last_cleanup_date") or "") != today:
                        # 先落盘日期，避免重复触发；任务内部自带失败重试
                        self._state["last_cleanup_date"] = today
                        self._save_state()
                        logger.info("[审核群] 开始每日清理（%s）。", today)
                        self._spawn(self._cleanup_job(reason="自动"))
                    target += timedelta(days=1)
                delay = (target - self._now()).total_seconds()
                await self._sleep(min(max(delay, 1.0), 60.0))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[审核群] 每日清理循环出现异常")
                await self._sleep(60)

    async def _rotate_code(self, reason: str) -> str:
        async with self._lock:
            code = self._generate_code()
            self._state["code"] = code
            self._state["code_date"] = self._today_str()
            self._state["code_created_at"] = time.time()
            self._save_state()
        logger.info("[审核群] 验证码已重置（%s）。", reason)
        return code

    @staticmethod
    def _validate_code(raw: str) -> str:
        """校验管理员手动指定的验证码：1~64 个不含空白的字符。"""
        code = str(raw or "").strip()
        if not code:
            raise ValueError("验证码不能为空")
        if len(code) > 64:
            raise ValueError("验证码最长 64 个字符")
        if any(ch.isspace() for ch in code):
            raise ValueError("验证码不能包含空格")
        return code

    async def _set_code(self, raw: str, actor: str) -> tuple[str, int]:
        """把验证码设定为指定值，返回（新验证码, 仍持有旧码的已通过人数）。

        手动设定同样会刷新签发日期，所以当天不会再被定时重置覆盖。
        """
        code = self._validate_code(raw)
        async with self._lock:
            approved = self._state.get("approved") or {}
            stale = sum(
                1 for record in approved.values() if str(record.get("code") or "") != code
            )
            self._state["code"] = code
            self._state["code_date"] = self._today_str()
            self._state["code_created_at"] = time.time()
            self._save_state()
        logger.info("[审核群] 验证码已被 %s 手动设定（出于安全考虑不记录验证码本身）。", actor)
        return code, stale

    async def _set_code_reply(self, event: AstrMessageEvent, raw: str) -> str:
        """手动设定验证码的统一回执（指令组子命令与顶层指令共用）。"""
        if not str(raw or "").strip():
            current = str(self._state.get("code") or "（未生成）")
            return (
                "用法：/审核 设定码 <验证码>（也可用 /设定审核码 <验证码>）\n"
                f"当前验证码：{current}"
            )
        try:
            code, stale = await self._set_code(raw, str(event.get_sender_id() or "未知管理员"))
        except ValueError as exc:
            return f"⚠️ 设定失败：{exc}"
        lines = [
            f"✅ 验证码已设定为：{code}",
            f"失效时间：{self._expire_text()}",
        ]
        if stale:
            lines.append(
                f"注意：今日已有 {stale} 名已通过审核的成员持有旧验证码，"
                "如需他们改用新码，可用 /审核 放行 <QQ号> 重新下发。"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ 清理逻辑

    async def _cleanup_job(
        self,
        *,
        groups: list[str] | None = None,
        reason: str = "手动",
        report_umo: str = "",
    ) -> dict[str, Any]:
        """执行一次清理，返回统计信息；自动清理会重试失败的群并重置审核记录。"""
        targets = list(groups if groups is not None else self._review_groups())
        summary: dict[str, Any] = {"groups": {}, "kicked": 0, "failed": []}
        if not targets:
            if report_umo:
                await self._send_umo(report_umo, "⚠️ 未配置审核群（review_groups），无事可做。")
            return summary

        await self._refresh_login_ids()
        await self._cleanup_targets(targets, summary)

        if reason == "自动":
            for _ in range(3):
                if not summary["failed"] or self._stop.is_set():
                    break
                logger.warning("[审核群] 以下群清理失败，5 分钟后重试：%s", "、".join(summary["failed"]))
                await self._sleep(300)
                await self._cleanup_targets(list(summary["failed"]), summary)
            await self._after_cleanup()

        if report_umo:
            lines = [f"🧹 清理完成（{reason}）", f"共移出 {summary['kicked']} 名成员"]
            for group_id, count in summary["groups"].items():
                lines.append(f"· 群 {group_id}：移出 {count} 人")
            if summary["failed"]:
                lines.append("⚠️ 以下群清理失败（机器人可能不是群管理员）：" + "、".join(summary["failed"]))
            await self._send_umo(report_umo, "\n".join(lines))
        return summary

    async def _cleanup_targets(self, targets: list[str], summary: dict[str, Any]) -> None:
        for group_id in targets:
            ok, kicked, error = await self._cleanup_group(group_id)
            summary["groups"][group_id] = summary["groups"].get(group_id, 0) + kicked
            summary["kicked"] += kicked
            if ok:
                if group_id in summary["failed"]:
                    summary["failed"].remove(group_id)
                notice = self._template("cleanup_notice").strip()
                if notice and kicked > 0:
                    await self._send_to_group(
                        group_id,
                        text=self._render(notice, count=kicked, group=group_id),
                    )
            else:
                if group_id not in summary["failed"]:
                    summary["failed"].append(group_id)
                logger.warning("[审核群] 群 %s 清理失败：%s", group_id, error)

    async def _cleanup_group(self, group_id: str) -> tuple[bool, int, str]:
        members = await self._member_list(group_id)
        if members is None:
            return False, 0, "无法获取群成员列表"
        kicked = 0
        for member in members:
            if not self._should_kick(group_id, member):
                continue
            user_id = str(member.get("user_id") or "")
            if await self._kick_member(group_id, user_id):
                kicked += 1
            await self._sleep(self._float("kick_interval_seconds", 1.0, minimum=0.0))
        return True, kicked, ""

    def _should_kick(self, group_id: str, member: dict) -> bool:
        user_id = str(member.get("user_id") or "")
        if not user_id or user_id in self._self_ids():
            return False
        role = str(member.get("role") or "member")
        if role in {"owner", "admin"} and self._bool("keep_group_admins", True):
            return False
        if user_id in self._admin_ids() or user_id in self._exempt_ids():
            return False
        if self._bool("cleanup_keep_approved", False):
            return self._key(group_id, user_id) not in (self._state.get("approved") or {})
        return True

    async def _after_cleanup(self) -> None:
        """每日清理后复位审核记录：新的一天是全新的一批人。"""
        async with self._lock:
            self._state["pending"] = {}
            if not self._bool("cleanup_keep_approved", False):
                self._state["approved"] = {}
            self._save_state()
        logger.info("[审核群] 每日清理完成，已复位待审核与已通过记录。")

    # ------------------------------------------------------------------ 入群与答题

    # 用 ALL 而不是 GROUP_MESSAGE：群通知在部分 AstrBot 版本里会被标成 OTHER_MESSAGE，
    # 那样处理器"永远不会被激活"且毫无日志。这里放开接收，再按 post_type/群号自己判断。
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_notice(self, event: AstrMessageEvent) -> None:
        """群通知事件：新人入群发起审核，成员退群清理记录。"""
        try:
            await self._handle_notice(event)
        except Exception:
            logger.exception("[审核群] 处理群通知事件失败")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """审核群内的普通消息：作为审核回答进行判定。"""
        try:
            await self._handle_answer_message(event)
        except Exception:
            logger.exception("[审核群] 处理审核回答失败")

    async def _handle_notice(self, event: AstrMessageEvent) -> None:
        self._ensure_ready()
        raw = _raw_dict(event)
        if str(raw.get("post_type") or "") != "notice":
            return
        notice_type = str(raw.get("notice_type") or "")
        if notice_type != "group_increase" and not notice_type.startswith("group_decrease"):
            logger.debug("[审核群] 忽略无关群通知：%s（群 %s）", notice_type, raw.get("group_id"))
            return

        group_id = str(event.get_group_id() or raw.get("group_id") or "")
        user_id = str(event.get_sender_id() or raw.get("user_id") or "")
        self._diag_bump("notices")
        if not group_id or group_id not in self._review_groups():
            self._diag_bump("other_group")
            configured = "、".join(self._review_groups()) or "（未配置）"
            self._diag_set_reason(f"通知来自群 {group_id or '未知'}，不在 review_groups（{configured}）中")
            logger.info("[审核群] 收到群通知但该群未配置，已忽略：群 %s（已配置：%s）", group_id or "未知", configured)
            return

        self._remember_event(event, group_id)
        if not user_id:
            self._diag_set_reason(f"群 {group_id} 的 {notice_type} 通知里缺少 user_id，无法审核")
            logger.warning("[审核群] 群 %s 的 %s 通知缺少 user_id，已忽略。", group_id, notice_type)
            return
        if user_id in self._self_ids():
            return
        if notice_type == "group_increase":
            self._diag_bump("increase")
            self._diag["last_notice"] = {
                "at": time.time(),
                "type": notice_type,
                "group_id": group_id,
                "user_id": user_id,
            }
            logger.info("[审核群] 收到入群通知：群 %s 用户 %s，进入审核流程。", group_id, user_id)
            await self._start_review(event, group_id, user_id)
        else:
            self._diag_bump("decrease")
            logger.info("[审核群] 收到退群通知：群 %s 用户 %s，清理审核记录。", group_id, user_id)
            await self._forget_pending(group_id, user_id)

    async def _handle_answer_message(self, event: AstrMessageEvent) -> None:
        self._ensure_ready()
        raw = _raw_dict(event)
        if str(raw.get("post_type") or "message") != "message":
            return
        group_id = str(event.get_group_id() or "")
        if not group_id or group_id not in self._review_groups():
            return
        self._remember_event(event, group_id)
        user_id = str(event.get_sender_id() or "")
        text = (event.message_str or "").strip()
        self._diag_bump("messages")
        self._diag["last_message"] = {
            "at": time.time(),
            "group_id": group_id,
            "user_id": user_id,
            "text": text[:40],
        }
        if not user_id or not text:
            return
        if user_id in self._self_ids() or user_id in self._admin_ids() or user_id in self._exempt_ids():
            return
        if _has_command_handler(event):
            return  # 这是给某个指令的消息，交给指令处理
        await self._judge_answer(event, group_id, user_id, text)

    async def _start_review(self, event: AstrMessageEvent, group_id: str, user_id: str) -> None:
        if user_id in self._self_ids():
            return
        if user_id in self._admin_ids() or user_id in self._exempt_ids():
            self._diag_bump("exempt")
            self._diag_set_reason(f"用户 {user_id} 在 admin_ids/exempt_user_ids 中，按配置跳过审核")
            logger.info("[审核群] %s 在管理员/免审核名单中，跳过审核。", user_id)
            return
        question = self._pick_question()
        if question is None:
            self._diag_bump("no_question")
            self._diag_set_reason("questions 里没有「同时填写了问题和答案」的题目")
            logger.error(
                "[审核群] 未配置可用的审核问题（questions 需同时填写问题和答案），已跳过 %s 的审核。",
                user_id,
            )
            return
        key = self._key(group_id, user_id)
        async with self._lock:
            existing = self._state["pending"].get(key)
            if existing is not None:
                # 正常流程中退群会清掉记录；能走到这里说明漏收了退群通知（例如机器人当时离线）。
                # 重新入群必须重新提问，否则新人会一句话都收不到。
                existing["attempts"] = 0
                existing["asked_at"] = time.time()
                existing["question"] = str(question.get("question") or "")
                existing["answers"] = [str(item) for item in (question.get("answers") or [])]
                existing["platform_id"] = str(event.get_platform_id() or existing.get("platform_id") or "")
                self._save_state()
                self._diag_bump("reask")
                logger.info("[审核群] %s 已有待审核记录（此前可能漏收退群通知），本次入群重新提问。", user_id)
            else:
                self._state["pending"][key] = self._new_record(
                    group_id,
                    user_id,
                    question,
                    event.get_platform_id(),
                )
                self._save_state()
        await self._ask_question(event, user_id, question, "新成员入群")

    async def _judge_answer(self, event: AstrMessageEvent, group_id: str, user_id: str, text: str) -> None:
        key = self._key(group_id, user_id)
        record = self._state["pending"].get(key)
        created = False
        if record is None:
            if not self._bool("auto_enroll_on_speak", True):
                return
            question = self._pick_question()
            if question is None:
                logger.error("[审核群] 未配置可用的审核问题（questions/answers 为空），已跳过 %s 的审核。", user_id)
                return
            is_group_admin = await self._member_role(group_id, user_id) in {"owner", "admin"}
            if self._bool("keep_group_admins", True) and is_group_admin:
                return
            created = True
            entry = question
        else:
            entry = {
                "question": str(record.get("question") or ""),
                "answers": [str(item) for item in (record.get("answers") or [])],
                "match_mode": str(record.get("match_mode") or "inherit"),
                "hint": str(record.get("hint") or ""),
            }

        # 判定放在锁外：LLM 审核是一次网络请求，不能占着状态锁阻塞其它成员的消息
        passed, source = await self._judge_text(event, group_id, user_id, entry, text)

        plan: dict[str, Any] = {}
        async with self._lock:
            current = self._state["pending"].get(key)
            if current is None:
                if not created:
                    return  # 已被其它事件（踢出/放行/退群）处理掉
                current = self._new_record(group_id, user_id, entry, event.get_platform_id())
                self._state["pending"][key] = current
            record = current

            if passed:
                code = str(self._state.get("code") or "")
                self._state["pending"].pop(key, None)
                self._state["approved"][key] = {
                    "at": time.time(),
                    "code": code,
                    "platform_id": str(record.get("platform_id") or ""),
                    "question": str(record.get("question") or ""),
                }
                self._bump_question_stat(str(record.get("question") or ""), "passes")
                self._save_state()
                plan = {
                    "action": "pass",
                    "code": code,
                    "platform_id": str(record.get("platform_id") or ""),
                    "source": source,
                }
            elif created:
                # 补发问题：不消耗答题次数，避免把成员的普通发言当成答错
                self._save_state()
                plan = {
                    "action": "ask",
                    "question": str(record.get("question") or ""),
                    "hint": str(record.get("hint") or ""),
                }
            else:
                record["attempts"] = int(record.get("attempts") or 0) + 1
                remaining = self._max_attempts() - int(record["attempts"])
                if remaining > 0:
                    self._save_state()
                    plan = {"action": "retry", "remaining": remaining, "source": source}
                else:
                    self._state["pending"].pop(key, None)
                    self._save_state()
                    plan = {"action": "kick", "source": source}

        event.stop_event()  # 这条消息归审核流程，不再触发其它 handler 与 LLM
        user_name = event.get_sender_name() or user_id
        action = plan.get("action")
        source_label = str(plan.get("source") or "规则判定")

        if action == "ask":
            question = {
                "question": plan.get("question", ""),
                "answers": [],
                "hint": plan.get("hint", ""),
            }
            await self._ask_question(event, user_id, question, "补发问题")
        elif action == "retry":
            text_out = self._render(
                self._template("retry_message"),
                user=user_name,
                remaining=plan.get("remaining", 0),
                max_attempts=self._max_attempts(),
            )
            await self._send_event_chain(event, [At(qq=user_id, name=""), Plain(f" {text_out}")])
            logger.info("[审核群] 成员 %s（群 %s）判定为未通过（%s），剩余 %s 次机会。", user_id, group_id, source_label, plan.get("remaining"))
        elif action == "pass":
            code = str(plan.get("code") or "")
            delivery = await self._deliver_code(
                group_id=group_id,
                user_id=user_id,
                platform_id=str(plan.get("platform_id") or event.get_platform_id()),
                code=code,
                user_name=user_name,
                event=event,
            )
            logger.info(
                "[审核群] 成员 %s 通过审核（群 %s，判定来源：%s，私聊发码：%s，群内发码：%s）。",
                user_id,
                group_id,
                source_label,
                "成功" if delivery.get("private") else "失败/未启用",
                "是" if delivery.get("group_code") else "否",
            )
        elif action == "kick":
            kicked = True
            if self._bool("kick_on_fail", True):
                kicked = await self._kick_member(group_id, user_id)
            kick_text = self._render(
                self._template("kick_message"),
                user=user_name,
                max_attempts=self._max_attempts(),
            )
            await self._send_event_chain(event, [Plain(kick_text)])
            logger.info(
                "[审核群] 成员 %s 在群 %s 审核失败（%s 次，判定来源：%s），%s。",
                user_id,
                group_id,
                self._max_attempts(),
                source_label,
                "已移出" if kicked else "移出失败（机器人可能不是管理员）",
            )

    def _review_mode(self) -> str:
        mode = str(self._get("review_mode", "rule") or "rule").strip().lower()
        return mode if mode in {"rule", "llm", "hybrid"} else "rule"

    def _code_send_mode(self) -> str:
        mode = str(self._get("code_send_mode", "private") or "private").strip().lower()
        return mode if mode in CODE_SEND_MODES else "private"

    async def _judge_text(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        entry: dict[str, Any],
        text: str,
    ) -> tuple[bool, str]:
        """返回（是否通过, 判定来源说明）。"""
        question = str(entry.get("question") or "")
        answers = [str(item) for item in (entry.get("answers") or [])]
        mode = self._resolve_match_mode(entry.get("match_mode") or "inherit")
        rule_pass, detail = self._evaluate_rules(text, answers, mode, self._common_answers())

        review_mode = self._review_mode()
        if review_mode == "rule":
            return rule_pass, f"规则·{detail}"
        if review_mode == "hybrid" and rule_pass:
            return True, f"规则·{detail}"  # 规则先命中，省一次模型调用

        verdict = await self._llm_judge(event, group_id, user_id, question, answers, text)
        if verdict is None:
            # 模型不可用/超时/答复无法解析：回退规则判定，绝不让成员卡在流程里
            logger.warning("[审核群] LLM 审核未得出结论，已回退规则判定（群 %s 用户 %s）。", group_id, user_id)
            return rule_pass, f"规则回退·{detail}"
        return verdict, "大模型判定"

    async def _llm_judge(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        question: str,
        answers: list[str],
        text: str,
    ) -> bool | None:
        provider = await self._resolve_llm_provider(event)
        if provider is None:
            return None
        prompt = self._render(
            self._template("llm_review_prompt"),
            question=question,
            answers="、".join(answers) if answers else "（未提供参考答案）",
            answer=text,
            user=event.get_sender_name() or user_id,
            group=group_id,
        )
        timeout = self._float("llm_review_timeout_seconds", 20.0, minimum=1.0)
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=LLM_JUDGE_SYSTEM_PROMPT),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[审核群] LLM 审核超时（>%s 秒），本次回退规则判定。", timeout)
            return None
        except Exception:
            logger.exception("[审核群] LLM 审核调用失败，本次回退规则判定")
            return None

        raw = str(getattr(response, "completion_text", "") or "")
        verdict = _parse_llm_verdict(raw)
        self._diag["last_llm_reply"] = raw.strip()[:300]
        logger.debug("[审核群] LLM 审核原始回复：%r -> %s", raw[:200], verdict)
        return verdict

    async def _resolve_llm_provider(self, event: AstrMessageEvent) -> Any:
        provider_id = str(self._get("review_llm_provider", "") or "").strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
            if provider is None:
                logger.error("[审核群] 配置的 review_llm_provider=%s 不存在，本次回退规则判定。", provider_id)
                return None
            if not hasattr(provider, "text_chat"):
                logger.error("[审核群] 配置的 review_llm_provider=%s 不是文本对话模型，本次回退规则判定。", provider_id)
                return None
            return provider
        try:
            provider = await self.context.get_using_provider_async(umo=event.unified_msg_origin)
        except Exception:
            logger.exception("[审核群] 获取当前会话模型失败，本次回退规则判定")
            return None
        if provider is None or not hasattr(provider, "text_chat"):
            logger.error(
                "[审核群] 当前会话没有可用的文本对话模型，本次回退规则判定"
                "（可在插件配置里显式指定 review_llm_provider）。",
            )
            return None
        return provider

    async def _deliver_code(
        self,
        *,
        group_id: str,
        user_id: str,
        platform_id: str,
        code: str,
        user_name: str = "",
        event: AstrMessageEvent | None = None,
    ) -> dict[str, bool]:
        """按 code_send_mode 发码：私聊 / 群内 / 两者，返回实际发送情况。"""
        mode = self._code_send_mode()
        expire = self._expire_text()
        display_name = user_name or user_id
        want_private = mode in {"private", "both"}
        want_group_code = mode in {"group", "both"}
        result = {"private": False, "group_code": want_group_code}

        if want_private:
            private_text = self._template("private_code_message").strip()
            if not private_text:
                private_text = "你的审核验证码是：{code}（有效期至 {expire}）"
            result["private"] = await self._send_private(
                str(platform_id or ""),
                user_id,
                self._render(private_text, code=code, expire=expire, user=display_name, group=group_id),
                group_id=group_id,
            )
            result["temp_session"] = result["private"] and self._private_send_channel() != "friend"
            if not result["private"]:
                logger.warning(
                    "[审核群] 私聊发码失败（群 %s 用户 %s）：可开启 code_fallback_to_group 兜底，"
                    "或用「/审核 补发 %s」重发。",
                    group_id,
                    user_id,
                    user_id,
                )
                if not want_group_code and self._bool("code_fallback_to_group", False):
                    want_group_code = True
                    result["group_code"] = True
                    logger.info("[审核群] 已按 code_fallback_to_group 改为群内发码（用户 %s）。", user_id)

        if group_id:
            group_text = self._render(
                self._template("success_message"),
                user=display_name,
                code=code,
                expire=expire,
                group=group_id,
            )
            if want_group_code and code and code not in group_text:
                # 群内发码但文案里没有 {code}：补一行，避免"配好了却没发码"
                group_text = f"{group_text}\n（验证码：{code}）" if group_text.strip() else f"验证码：{code}"
                if not self._group_code_notice_logged:
                    self._group_code_notice_logged = True
                    logger.info(
                        "[审核群] code_send_mode=%s 需要群内发码，但 success_message 里没有 {code}，已自动补一行；"
                        "建议把 {code} 直接写进 success_message。",
                        mode,
                    )
            elif not want_group_code and code and code in group_text and not self._private_leak_warned:
                # 反向的坑：只走私聊，但文案里还留着 {code}，验证码会被念在群里
                self._private_leak_warned = True
                logger.warning(
                    "[审核群] code_send_mode=%s 只走私聊发码，但 success_message 里含 {code}，"
                    "验证码会出现在群里。请把 success_message 里的 {code} 删掉。",
                    mode,
                )
            await self._send_to_group(group_id, components=[At(qq=user_id, name=""), Plain(f" {group_text}")])

        if not group_id and not result["private"]:
            logger.warning("[审核群] 无群号且私聊失败，验证码未能送达用户 %s。", user_id)
        return result

    async def _ask_question(self, event: AstrMessageEvent, user_id: str, question: dict[str, Any], scene: str) -> bool:
        text = self._render(
            self._template("question_message"),
            user=event.get_sender_name() or user_id,
            question=str(question.get("question") or ""),
            max_attempts=self._max_attempts(),
        )
        hint = str(question.get("hint") or "").strip()
        if hint and hint not in text:
            # 该题配了提示但文案里没写 {hint}：直接附一行，避免"配了不生效"
            text = f"{text}\n提示：{hint}"
        sent = await self._send_event_chain(event, [At(qq=user_id, name=""), Plain(f" {text}")])
        if sent:
            self._diag_bump("asked")
            self._diag["last_question"] = {
                "at": time.time(),
                "user_id": user_id,
                "group_id": str(event.get_group_id() or ""),
                "scene": scene,
            }
            self._bump_question_stat(str(question.get("question") or ""), "picks")
            self._save_state()
            logger.info(
                "[审核群] 已向 %s 发送审核问题（%s，匹配方式 %s）。",
                user_id,
                scene,
                self._resolve_match_mode(question.get("match_mode") or "inherit"),
            )
        else:
            self._diag_bump("send_fail")
            self._diag_set_reason("向群里发送审核问题失败（机器人可能被禁言/被风控）")
            logger.error("[审核群] 向 %s 发送审核问题失败（%s）。", user_id, scene)
        return sent

    async def _forget_pending(self, group_id: str, user_id: str) -> None:
        key = self._key(group_id, user_id)
        async with self._lock:
            removed = self._state["pending"].pop(key, None)
            self._state["approved"].pop(key, None)
            if removed is not None:
                self._save_state()

    def _match_answer(self, text: str, answers: list[str]) -> bool:
        """按全局匹配方式判定（供指令/自检使用的简版入口）。"""
        passed, _ = self._match_with(text, answers, self._match_mode(), self._fuzzy_threshold())
        return passed

    def _match_with(self, text: str, answers: list[str], mode: str, threshold: float) -> tuple[bool, str]:
        """单组答案的匹配，返回（是否命中, 可读原因）。"""
        keys = [str(answer).strip() for answer in answers if str(answer).strip()]
        if not keys:
            return False, "该题为空答案"
        if mode == "regex":
            for pattern in keys:
                try:
                    if re.search(pattern, text):
                        return True, f"正则 {pattern!r} 命中"
                except re.error:
                    logger.warning("[审核群] 正则答案不合法，已忽略：%r", pattern)
            return False, "没有匹配的正则"
        normalized_text = _normalize_text(text)
        if not normalized_text:
            return False, "回答为空"
        normalized_keys = [(key, _normalize_text(key)) for key in keys]
        normalized_keys = [(key, norm) for key, norm in normalized_keys if norm]

        if mode == "exact":
            for key, norm in normalized_keys:
                if normalized_text == norm:
                    return True, f"完全匹配「{key}」"
            return False, "没有完全相等的答案"

        for key, norm in normalized_keys:  # contains（fuzzy 也先走这一步）
            if norm in normalized_text:
                return True, f"包含「{key}」"

        if mode == "fuzzy":
            best_ratio, best_key = 0.0, ""
            for key, norm in normalized_keys:
                ratio = difflib.SequenceMatcher(None, normalized_text, norm).ratio()
                if ratio > best_ratio:
                    best_ratio, best_key = ratio, key
            if best_ratio >= threshold:
                return True, f"模糊匹配「{best_key}」相似度 {best_ratio:.2f} ≥ {threshold:.2f}"
            return False, f"最接近的「{best_key}」相似度 {best_ratio:.2f} < {threshold:.2f}"
        return False, "没有包含任何答案关键词"

    def _evaluate_rules(
        self,
        text: str,
        answers: list[str],
        mode: str,
        common: list[str] | None = None,
    ) -> tuple[bool, str]:
        """先判该题答案库，再判通用答案库。返回（是否通过, 原因）。"""
        passed, detail = self._match_with(text, answers, mode, self._fuzzy_threshold())
        if passed:
            return True, detail
        if common:
            common_passed, common_detail = self._match_with(text, common, mode, self._fuzzy_threshold())
            if common_passed:
                return True, f"通用答案库：{common_detail}"
        return False, detail

    def _explain_rules(self, text: str, entry: dict[str, Any]) -> list[str]:
        """逐条列出答案命中情况，供 /审核 试答 调答案库用。"""
        mode = self._resolve_match_mode(entry.get("match_mode") or "inherit")
        threshold = self._fuzzy_threshold()
        normalized = _normalize_text(text)
        lines = [f"匹配方式：{mode}（{'全局' if str(entry.get('match_mode') or 'inherit') == 'inherit' else '本题覆盖'}）"
                 f" ｜ 归一化后：{normalized or '（空）'}"]
        for label, group in (("题目答案", list(entry.get("answers") or [])), ("通用答案", self._common_answers())):
            if not group:
                continue
            lines.append(f"{label}库：{'、'.join(group)}")
            for answer in group:
                one_passed, detail = self._match_with(text, [answer], mode, threshold)
                marker = "✅" if one_passed else "❌"
                lines.append(f"  {marker} {answer} → {detail}")
        return lines

    # ------------------------------------------------------------------ 管理指令

    @filter.command("审核码", alias={"验证码", "审核验证码"})
    async def cmd_review_code(self, event: AstrMessageEvent):
        """查询当前审核验证码"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以查询验证码。")
            return
        yield event.plain_result(self._code_report())

    @filter.command("设定审核码", alias={"设定验证码"})
    async def cmd_set_review_code(self, event: AstrMessageEvent, code: str = ""):
        """手动设定当日验证码"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以设定验证码。")
            return
        yield event.plain_result(await self._set_code_reply(event, code))

    @filter.command_group("审核")
    def review_cmd(self):
        """临时审核群管理指令组"""

    @review_cmd.command("帮助")
    async def review_help(self, event: AstrMessageEvent):
        """显示审核群管理帮助"""
        yield event.plain_result(HELP_TEXT)

    @review_cmd.command("状态")
    async def review_status(self, event: AstrMessageEvent):
        """查看验证码、待审核成员与已通过成员"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以查看审核状态。")
            return
        lines = [
            "📋 临时审核群状态",
            f"审核群：{'、'.join(self._review_groups()) or '未配置'}",
            "",
            self._code_report(),
        ]
        pending = list((self._state.get("pending") or {}).items())
        lines.append("")
        if pending:
            lines.append(f"⏳ 待审核（共 {len(pending)} 人，最多显示 20 条）：")
            for _, record in pending[:20]:
                lines.append(
                    f"· 群 {record.get('group_id')} ｜ QQ {record.get('user_id')} ｜ 已答错 "
                    f"{record.get('attempts', 0)}/{self._max_attempts()}"
                    f" ｜ 提问于 {self._fmt_ts(record.get('asked_at'))}"
                )
        else:
            lines.append("⏳ 当前没有待审核成员。")

        approved = list((self._state.get("approved") or {}).items())
        if approved:
            lines.append("")
            lines.append(f"✅ 已通过（共 {len(approved)} 人，最多显示 20 条）：")
            for key, record in approved[:20]:
                group_id, _, user_id = str(key).partition(":")
                lines.append(f"· 群 {group_id} ｜ QQ {user_id} ｜ 通过于 {self._fmt_ts(record.get('at'))}")
        yield event.plain_result("\n".join(lines))

    @review_cmd.command("诊断")
    async def review_diagnose(self, event: AstrMessageEvent):
        """排查「新人入群没有收到消息」：/审核 诊断"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以执行诊断。")
            return
        counters = dict(self._diag.get("counters") or {})
        groups = self._review_groups()
        current_group = str(event.get_group_id() or "")
        usable = [item for item in self._questions() if item["question"] and item["answers"]]
        platform_ids = [platform_id for platform_id, _ in self._aiocqhttp_clients()]
        lines = [
            "🩺 审核群诊断（计数自插件本次加载起累计，重载后清零）",
            f"插件：{PLUGIN_NAME} {PLUGIN_VERSION}",
            f"数据文件：{self._state_path or '不可用（无法写入状态，可能是权限问题）'}",
            f"审核群配置：{'、'.join(groups) or '（未配置）'}",
            f"当前群：{current_group or '（私聊）'} ｜ "
            + ("已在配置中 ✅" if current_group and current_group in groups else "不在配置中 ❌"),
            f"aiocqhttp 平台：{'、'.join(platform_ids) or '（无：请检查 AstrBot 平台适配器）'}",
            f"审核题目：{f'可用 {len(usable)} 条' if usable else '不可用 ❌（每条都要同时填写问题与答案）'}",
            f"判定/发码：{self._review_mode()} ｜ {self._code_send_mode()} / {self._private_send_channel()}",
            f"管理指令权限：{'✅' if await self._check_admin(event) else '❌'}",
            "",
            "事件计数：",
            f"· 群通知 {counters.get('notices', 0)} 条（入群 {counters.get('increase', 0)} ｜ "
            f"退群 {counters.get('decrease', 0)} ｜ 非配置群 {counters.get('other_group', 0)}）",
            f"· 配置群内消息 {counters.get('messages', 0)} 条",
            f"· 已发送问题 {counters.get('asked', 0)} ｜ 重新提问 {counters.get('reask', 0)} ｜ "
            f"发送失败 {counters.get('send_fail', 0)} ｜ 题目不可用 {counters.get('no_question', 0)} ｜ "
            f"名单跳过 {counters.get('exempt', 0)}",
            f"· 最近通知：{self._diag_fmt(self._diag.get('last_notice'))}",
            f"· 最近群消息：{self._diag_fmt(self._diag.get('last_message'))}",
            f"· 最近提问：{self._diag_fmt(self._diag.get('last_question'))}",
        ]
        reason = str(self._diag.get("last_reason") or "")
        if reason:
            lines.append(f"· 最近一次跳过原因：{reason}")
        lines.append("")
        lines.append("结论：" + self._diag_verdict(counters))

        target = current_group if current_group in groups else (groups[0] if groups else "")
        if target:
            sent = await self._send_to_group(
                target,
                text="【自检】审核群插件测试消息：能看到这条，说明机器人可以向本群发消息。",
            )
            lines.append(
                f"发送自检：向群 {target} 发消息 "
                + ("成功 ✅" if sent else "失败 ❌（检查机器人是否在群内/被禁言/协议端是否正常）")
            )
        else:
            lines.append("发送自检：跳过（未配置审核群，请在插件配置里填写 review_groups）")
        lines.append("")
        lines.append(
            "排查提示：① 改完配置要在 WebUI 重载插件；② 让一个人**重新进群**才会触发入群通知，"
            f"已在群里的人不会有通知；③ 也可以直接 `/审核 重审 <QQ号>` 手动给他提一次问题；"
            "④ 若「群通知」一直是 0 而「群内消息」在涨，说明协议端没有上报群事件。"
        )
        yield event.plain_result("\n".join(lines))

    @review_cmd.command("题库")
    async def review_bank(self, event: AstrMessageEvent):
        """查看问题库与答案库（含抽中/通过统计）：/审核 题库"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以查看题库。")
            return
        entries = self._questions(include_disabled=True)
        global_mode = self._match_mode()
        stats = self._state.get("question_stats") or {}
        common = self._common_answers()
        lines = [
            "📚 问题库 / 答案库",
            f"全局匹配方式：{global_mode} ｜ 模糊阈值：{self._fuzzy_threshold():.2f} ｜ 判定：{self._review_mode()}",
            f"通用答案库（任何题目命中即通过）：{'、'.join(common) or '（未配置）'}",
            "",
        ]
        if not entries:
            lines.append("（问题库为空：请在插件配置的 questions 里添加题目）")
        usable_count = 0
        for index, entry in enumerate(entries, start=1):
            stat = stats.get(entry["question"]) or {}
            mode = entry["match_mode"]
            mode_text = f"inherit→{global_mode}" if mode == "inherit" else mode
            usable = entry["enabled"] and self._question_usable(entry)
            if usable:
                usable_count += 1
            lines.append(
                f"{index}. {'✅' if usable else ('⛔ 已停用' if not entry['enabled'] else '⚠️ 不可用')} "
                f"{entry['question'] or '（题干为空）'}"
            )
            lines.append(f"   答案库：{'、'.join(entry['answers']) or '（空，只能靠通用答案库）'}")
            lines.append(
                f"   匹配：{mode_text} ｜ 提示：{entry['hint'] or '（无）'} ｜ "
                f"抽中 {stat.get('picks', 0)} 次 / 通过 {stat.get('passes', 0)} 次"
            )
        lines.append("")
        lines.append(f"可用题目：{usable_count} 条" + ("（为 0 时新人入群不会收到问题）" if not usable_count else ""))
        lines.append("用法：/审核 试答 #题号 你的回答 —— 先验证某句话会不会通过，再决定怎么改答案库。")
        yield event.plain_result("\n".join(lines))

    @review_cmd.command("试答")
    async def review_test_answer(self, event: AstrMessageEvent):
        """试答：验证某句话会不会通过。/审核 试答 [#题号] <回答>"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以试答。")
            return
        raw = (event.message_str or "").strip()
        if "试答" in raw:
            raw = raw.split("试答", 1)[1].strip()
        if not raw:
            yield event.plain_result("用法：/审核 试答 [#题号] <回答>\n例：/审核 试答 朋友推荐我来的 ｜ /审核 试答 #2 二")
            return

        entries = self._questions(include_disabled=True)
        if not entries:
            yield event.plain_result("⚠️ 问题库为空，无法试答。请先在插件配置里添加题目。")
            return
        index = 1
        match = re.match(r"^#?(\d{1,2})\s+(.+)$", raw, re.S)
        if match:
            index = int(match.group(1))
            raw = match.group(2).strip()
        if not 1 <= index <= len(entries):
            yield event.plain_result(f"⚠️ 题号超出范围（1~{len(entries)}）。用 /审核 题库 查看题号。")
            return

        entry = entries[index - 1]
        lines = [
            f"🧪 试答（第 {index} 题{'' if entry['enabled'] else '，已停用'}）",
            f"题目：{entry['question'] or '（题干为空）'}",
            f"回答：{raw}",
            "",
        ]
        lines.extend(self._explain_rules(raw, entry))
        passed, detail = self._evaluate_rules(
            raw,
            list(entry.get("answers") or []),
            self._resolve_match_mode(entry.get("match_mode") or "inherit"),
            self._common_answers(),
        )
        lines.append("")
        lines.append(f"规则判定：{'通过 ✅' if passed else '不通过 ❌'}（{detail}）")

        if self._review_mode() != "rule":
            lines.append("")
            lines.append("正在询问大模型…")
            yield event.plain_result("\n".join(lines))
            lines = []
            verdict = await self._llm_judge(
                event,
                str(event.get_group_id() or ""),
                str(event.get_sender_id() or ""),
                str(entry.get("question") or ""),
                list(entry.get("answers") or []),
                raw,
            )
            if verdict is None:
                lines.append("大模型判定：不可用/无法解析（正式审核会回退规则判定）")
            else:
                lines.append(f"大模型判定：{'通过 ✅' if verdict else '不通过 ❌'}")
            reply = str(self._diag.get("last_llm_reply") or "")
            if reply:
                lines.append(f"模型原始回复：{reply}")
        lines.append("")
        lines.append(
            "提示：这条试答不会消耗成员的答题次数、也不改任何记录；"
            "想调答案库就到插件配置的 questions / common_answers 里改。"
        )
        yield event.plain_result("\n".join(lines))

    @review_cmd.command("重置码")
    async def review_reset_code(self, event: AstrMessageEvent):
        """立即重新生成当日验证码"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以重置验证码。")
            return
        code = await self._rotate_code("管理员手动重置")
        yield event.plain_result(f"✅ 验证码已重置为：{code}\n失效时间：{self._expire_text()}")

    @review_cmd.command("设定码")
    async def review_set_code(self, event: AstrMessageEvent, code: str = ""):
        """手动设定当日验证码：/审核 设定码 <验证码>"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以设定验证码。")
            return
        yield event.plain_result(await self._set_code_reply(event, code))

    @review_cmd.command("放行")
    async def review_approve(self, event: AstrMessageEvent, target: str = "", group: str = ""):
        """手动放行成员并下发当日验证码：/审核 放行 <QQ号> [群号]"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以放行成员。")
            return
        user_id = self._extract_target(event, target)
        if not user_id:
            yield event.plain_result("用法：/审核 放行 <QQ号> [群号]（也可以 @ 该成员）")
            return
        group_id = self._pick_group(event, group)
        code = str(self._state.get("code") or "")
        async with self._lock:
            if group_id:
                key = self._key(group_id, user_id)
                self._state["pending"].pop(key, None)
                self._state["approved"][key] = {
                    "at": time.time(),
                    "code": code,
                    "platform_id": str(self._state.get("group_platform", {}).get(group_id) or ""),
                }
                self._save_state()
        if group_id:
            delivery = await self._deliver_code(
                group_id=group_id,
                user_id=user_id,
                platform_id=str(self._state.get("group_platform", {}).get(group_id) or event.get_platform_id()),
                code=code,
                user_name=user_id,
                event=event,
            )
        else:
            delivery = {"private": False, "group_code": False}
        tail = f"（群 {group_id}）" if group_id else "（未找到可用审核群，未发送群消息）"
        yield event.plain_result(
            f"✅ 已放行 QQ {user_id}{tail}\n"
            f"当前验证码：{code}\n"
            f"发码结果：私聊{'成功' if delivery.get('private') else '未发送/失败'}，"
            f"群内{'已发' if delivery.get('group_code') else '未发码'}",
        )

    @review_cmd.command("补发")
    async def review_resend_code(self, event: AstrMessageEvent, target: str = "", group: str = ""):
        """给成员补发当日验证码（私聊）：/审核 补发 <QQ号> [群号]"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以补发验证码。")
            return
        user_id = self._extract_target(event, target)
        if not user_id:
            yield event.plain_result("用法：/审核 补发 <QQ号> [群号]（也可以 @ 该成员）")
            return
        group_id = self._pick_group(event, group)
        platform_id = str(self._state.get("group_platform", {}).get(group_id) or event.get_platform_id())
        code = str(self._state.get("code") or "")
        private_text = self._template("private_code_message").strip() or "你的审核验证码是：{code}（有效期至 {expire}）"
        sent = await self._send_private(
            platform_id,
            user_id,
            self._render(private_text, code=code, expire=self._expire_text(), user=user_id, group=group_id),
            group_id=group_id,
        )
        if sent:
            yield event.plain_result(f"✅ 已私发验证码给 QQ {user_id}（当前码：{code}）")
        else:
            yield event.plain_result(
                f"❌ 私发失败：QQ {user_id} 需要与机器人在同一个群（走群临时会话），"
                "或已添加机器人为好友。\n"
                f"当前验证码：{code}（可自行转达，或用 /设定审核码 换一个更便于口头传达的码）",
            )

    @review_cmd.command("重审")
    async def review_retry(self, event: AstrMessageEvent, target: str = "", group: str = ""):
        """清空记录并重新提问：/审核 重审 <QQ号> [群号]"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以重置审核。")
            return
        user_id = self._extract_target(event, target)
        if not user_id:
            yield event.plain_result("用法：/审核 重审 <QQ号> [群号]（也可以 @ 该成员）")
            return
        group_id = self._pick_group(event, group)
        question = self._pick_question()
        async with self._lock:
            if group_id:
                key = self._key(group_id, user_id)
                self._state["pending"].pop(key, None)
                self._state["approved"].pop(key, None)
                if question is not None:
                    self._state["pending"][key] = self._new_record(
                        group_id,
                        user_id,
                        question,
                        str(self._state.get("group_platform", {}).get(group_id) or ""),
                    )
                self._save_state()
        if group_id and question is not None:
            text = self._render(
                self._template("question_message"),
                user=user_id,
                question=str(question.get("question") or ""),
                max_attempts=self._max_attempts(),
            )
            await self._send_to_group(group_id, components=[At(qq=user_id, name=""), Plain(f" {text}")])
            yield event.plain_result(f"🔁 已重置 QQ {user_id} 的审核记录并重新提问（群 {group_id}）。")
        elif question is None:
            yield event.plain_result("⚠️ 未配置可用的审核问题（questions/answers），无法重新提问。")
        else:
            yield event.plain_result("⚠️ 未找到可用审核群，未重新提问。请检查 review_groups 配置。")

    @review_cmd.command("踢出")
    async def review_kick(self, event: AstrMessageEvent, target: str = "", group: str = ""):
        """手动移出成员：/审核 踢出 <QQ号> [群号]"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以移出成员。")
            return
        user_id = self._extract_target(event, target)
        if not user_id:
            yield event.plain_result("用法：/审核 踢出 <QQ号> [群号]（也可以 @ 该成员）")
            return
        group_id = self._pick_group(event, group)
        if not group_id:
            yield event.plain_result("⚠️ 未配置审核群（review_groups）。")
            return
        ok = await self._kick_member(group_id, user_id)
        await self._forget_pending(group_id, user_id)
        yield event.plain_result(
            f"{'✅ 已移出' if ok else '❌ 移出失败（机器人可能不是群管理员）'} QQ {user_id}（群 {group_id}）"
        )

    @review_cmd.command("清理")
    async def review_cleanup(self, event: AstrMessageEvent, group: str = ""):
        """立即执行一次清理：/审核 清理 [群号]"""
        if not await self._check_admin(event):
            yield event.plain_result("⛔ 只有管理员可以执行清理。")
            return
        targets = [self._pick_group(event, group)] if group else self._review_groups()
        targets = [group_id for group_id in targets if group_id]
        if not targets:
            yield event.plain_result("⚠️ 未配置审核群（review_groups），无事可做。")
            return
        self._spawn(self._cleanup_job(groups=targets, reason="手动", report_umo=event.unified_msg_origin))
        yield event.plain_result("🧹 已开始清理：" + "、".join(targets) + "\n完成后会在当前会话输出统计结果。")

    # ------------------------------------------------------------------ 指令辅助

    async def _check_admin(self, event: AstrMessageEvent) -> bool:
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            return False
        if user_id in self._admin_ids():
            return True
        try:
            if event.role == "admin" or event.is_admin():
                return True
        except Exception:
            pass
        if not self._bool("group_admin_can_query", True):
            return False
        group_id = str(event.get_group_id() or "")
        if group_id and group_id in self._review_groups():
            return await self._member_role(group_id, user_id) in {"owner", "admin"}
        return False

    def _extract_target(self, event: AstrMessageEvent, raw: str) -> str:
        """从 @ 消息段或指令参数里取出目标 QQ 号。"""
        for component in event.get_messages():
            if type(component).__name__ != "At":
                continue
            qq = str(getattr(component, "qq", "") or "")
            if qq and qq not in {"all", str(event.get_self_id())}:
                return qq
        match = _DIGITS_RE.search(raw or "")
        return match.group(0) if match else ""

    def _pick_group(self, event: AstrMessageEvent, raw: str = "") -> str:
        """确定指令作用的群：显式指定 > 当前群（须是审核群）> 第一个审核群。"""
        groups = self._review_groups()
        match = _DIGITS_RE.search(raw or "")
        if match:
            return match.group(0)
        current = str(event.get_group_id() or "")
        if current and current in groups:
            return current
        return groups[0] if groups else ""

    def _code_report(self) -> str:
        code = str(self._state.get("code") or "（未生成）")
        lines = [
            f"🔐 当前验证码：{code}",
            f"签发时间：{self._fmt_ts(self._state.get('code_created_at'))}",
        ]
        target = self._next_reset_at()
        if target is None:
            lines.append("失效时间：未启用每日重置（仅管理员手动重置）")
        else:
            lines.append(
                f"失效时间：{target.strftime('%m-%d %H:%M')}"
                f"（剩余 {self._humanize((target - self._now()).total_seconds())}）"
            )
        pending = len(self._state.get("pending") or {})
        approved = len(self._state.get("approved") or {})
        lines.append(f"待审核：{pending} 人 ｜ 已通过：{approved} 人")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 插件 Pages 后端

    def _register_web_apis(self) -> None:
        """注册插件 Pages 使用的后端 API。路由必须带插件名前缀，前端 endpoint 不带。"""
        if not WEB_API_AVAILABLE:
            logger.warning("[审核群] 当前 AstrBot 不支持 astrbot.api.web，WebUI 设置页不可用。")
            return
        try:
            self.context.register_web_api(
                f"{WEB_API_PREFIX}/settings",
                self.api_get_settings,
                ["GET"],
                "读取插件配置与运行状态",
            )
            self.context.register_web_api(
                f"{WEB_API_PREFIX}/settings",
                self.api_save_settings,
                ["POST"],
                "校验并保存插件配置",
            )
            self.context.register_web_api(
                f"{WEB_API_PREFIX}/questions",
                self.api_get_questions,
                ["GET"],
                "读取问题库与答案库",
            )
            self.context.register_web_api(
                f"{WEB_API_PREFIX}/questions",
                self.api_save_questions,
                ["POST"],
                "保存问题库与答案库",
            )
            self.context.register_web_api(
                f"{WEB_API_PREFIX}/questions/parse",
                self.api_parse_questions,
                ["POST"],
                "用大模型把一段文本解析成题目与答案",
            )
        except Exception:
            logger.exception("[审核群] 注册 Web API 失败，WebUI 设置页将不可用。")

    def _load_schema(self) -> dict[str, Any]:
        """读取并缓存 _conf_schema.json（配置定义与页面表单的唯一来源）。"""
        if self._schema_cache is not None:
            return self._schema_cache
        path = Path(__file__).with_name(SCHEMA_FILE)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[审核群] 读取配置 schema 失败：%s", path)
            data = {}
        self._schema_cache = data if isinstance(data, dict) else {}
        return self._schema_cache

    @staticmethod
    def _secret_fields(schema: dict[str, Any]) -> list[str]:
        return [
            key
            for key, node in schema.items()
            if isinstance(node, dict) and node.get("secret")
        ]

    def _plugin_meta(self, page: str = "settings") -> dict[str, Any]:
        """插件展示信息：优先取 AstrBot 注册的元数据，读不到时回退到常量。"""
        meta: dict[str, Any] = {
            "name": PLUGIN_NAME,
            "display_name": PLUGIN_DISPLAY_NAME,
            "version": PLUGIN_VERSION,
            "description": "",
            "page": page,
        }
        try:
            metadata = self.context.get_registered_star(PLUGIN_NAME)
        except Exception:
            metadata = None
        if metadata is not None:
            meta["display_name"] = (
                getattr(metadata, "display_name", None)
                or getattr(metadata, "name", None)
                or meta["display_name"]
            )
            meta["version"] = getattr(metadata, "version", None) or meta["version"]
            meta["description"] = getattr(metadata, "desc", None) or ""
        return meta

    def _public_values(self, schema: dict[str, Any]) -> dict[str, Any]:
        """整理给前端的当前配置值；secret 字段只回空串，不回真实内容。"""
        config = self.config if isinstance(self.config, dict) else {}
        values: dict[str, Any] = {}
        for key, node in schema.items():
            if not isinstance(node, dict):
                continue
            current = config.get(key)
            if current is None and "default" in node:
                current = node["default"]
            values[key] = "" if node.get("secret") else current
        return values

    def _runtime_status(self) -> dict[str, Any]:
        """设置页顶部展示的只读运行状态。"""
        next_reset = self._next_reset_at()
        next_cleanup = self._next_time_at("cleanup_time", "00:00")
        return {
            "code": str(self._state.get("code") or ""),
            "code_created_at": self._fmt_ts(self._state.get("code_created_at")),
            "code_expire_at": next_reset.strftime("%Y-%m-%d %H:%M") if next_reset else "",
            "code_expire_text": self._expire_text(),
            "pending": len(self._state.get("pending") or {}),
            "approved": len(self._state.get("approved") or {}),
            "review_groups": self._review_groups(),
            "cleanup_time": self._time_str("cleanup_time", "00:00") or "",
            "code_reset_time": self._time_str("code_reset_time", "00:00") or "",
            "next_cleanup_at": next_cleanup.strftime("%Y-%m-%d %H:%M") if next_cleanup else "",
            "timezone": str(self._get("timezone", "") or ""),
            "server_time": self._now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_file": str(self._state_path or ""),
            "has_questions": self._pick_question() is not None,
            "platforms": [platform_id for platform_id, _ in self._aiocqhttp_clients()],
            "review_mode": self._review_mode(),
            "code_send_mode": self._code_send_mode(),
        }

    def _provider_options(self) -> list[dict[str, str]]:
        """可选的对话模型列表，用于设置页的提供商下拉框。"""
        options: list[dict[str, str]] = []
        try:
            providers = self.context.get_all_providers()
        except Exception:
            providers = []
        for provider in providers or []:
            try:
                meta = provider.meta()
            except Exception:
                continue
            provider_id = str(getattr(meta, "id", "") or "")
            if not provider_id:
                continue
            model = str(getattr(meta, "model", "") or "")
            options.append({"id": provider_id, "name": f"{provider_id}（{model}）" if model else provider_id})
        return options

    async def api_get_settings(self, *args: Any, **kwargs: Any):
        """GET <插件名>/settings：返回 schema、当前配置与运行状态。"""
        schema = self._load_schema()
        return json_response(
            {
                "status": "ok",
                "data": {
                    "schema": schema,
                    "values": self._public_values(schema),
                    "secret_fields": self._secret_fields(schema),
                    "status": self._runtime_status(),
                    "providers": self._provider_options(),
                    "meta": self._plugin_meta(),
                },
            },
        )

    async def api_save_settings(self, *args: Any, **kwargs: Any):
        """POST <插件名>/settings：校验并保存配置，返回最新值与提示。"""
        schema = self._load_schema()
        if not schema:
            return error_response("无法读取插件配置定义（_conf_schema.json）")
        payload = await self._read_json_payload(args, default=None)
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        values = payload.get("values")
        if not isinstance(values, dict):
            return error_response("请求体缺少 values 对象")
        if not values:
            return error_response("没有需要保存的配置项")

        secret_fields = set(self._secret_fields(schema))
        normalized: dict[str, Any] = {}
        errors: list[str] = []
        for key, raw in values.items():
            node = schema.get(key)
            if not isinstance(node, dict):
                errors.append(f"{key}：不是已知的配置项")
                continue
            if key in secret_fields and str(raw or "") == "":
                continue  # 留空表示沿用已保存的值
            try:
                value = self._coerce_value(key, node, raw)
            except ValueError as exc:
                errors.append(f"{key}：{exc}")
                continue
            semantic_error = self._semantic_error(key, value)
            if semantic_error:
                errors.append(f"{key}：{semantic_error}")
                continue
            normalized[key] = value
        if errors:
            return error_response(
                "配置校验未通过\n" + "\n".join(f"· {item}" for item in errors),
            )
        if not normalized:
            return error_response("没有需要保存的配置项")

        try:
            await self._persist_config(normalized)
        except Exception as exc:
            logger.exception("[审核群] 保存插件配置失败")
            return error_response(f"保存配置失败：{exc}", status_code=500)

        self._warn_config()
        logger.info(
            "[审核群] 配置已通过 WebUI 更新（操作者：%s）：%s",
            self._request_username(),
            "、".join(normalized),
        )
        return json_response(
            {
                "status": "ok",
                "data": {
                    "values": self._public_values(schema),
                    "saved": list(normalized),
                    "warnings": self._semantic_warnings(),
                },
            },
        )

    async def _persist_config(self, values: dict[str, Any]) -> None:
        """写回插件配置文件；优先异步保存，避免阻塞事件循环。"""
        save_async = getattr(self.config, "save_config_async", None)
        if callable(save_async):
            await save_async(values)
            return
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save(values)
            return
        raise RuntimeError("当前配置对象不支持保存，请改用 AstrBot 自带的插件配置页")

    @staticmethod
    async def _read_json_payload(args: tuple[Any, ...], default: Any) -> Any:
        """兼容两种分发方式：显式传入 request 对象，或用 astrbot.api.web.request 代理。"""
        for candidate in args:
            reader = getattr(candidate, "json", None)
            if not callable(reader):
                continue
            try:
                data = reader()
                if inspect.isawaitable(data):
                    data = await data
                return data
            except Exception:
                continue
        if request is not None:
            try:
                return await request.json(default=default)
            except Exception:
                return default
        return default

    @staticmethod
    def _request_username() -> str:
        if request is None:
            return "未知"
        try:
            return str(request.username or "未知")
        except Exception:
            return "未知"

    def _semantic_warnings(self) -> list[str]:
        """保存成功但配置明显不完整时的提醒（不阻止保存）。"""
        warnings: list[str] = []
        if not self._review_groups():
            warnings.append("review_groups 为空：插件不会执行任何审核或清理动作。")
        if self._pick_question() is None:
            warnings.append("questions 没有可用题目（需同时填写问题和答案）：新人入群不会收到审核问题。")
        if not self._admin_ids():
            warnings.append("admin_ids 为空：管理指令只对 AstrBot 全局管理员开放。")
        if self._review_mode() != "rule" and not self._provider_options():
            warnings.append("审核判定用了大模型，但当前没有任何可用的对话模型 Provider：会一直回退到规则判定。")
        if (
            self._review_mode() != "rule"
            and not str(self._get("review_llm_provider", "") or "").strip()
            and self._provider_options()
        ):
            warnings.append("没有指定 review_llm_provider：将跟随每个会话当前使用的模型，判定质量会随会话设置变化。")
        return warnings

    def _coerce_value(self, key: str, node: dict[str, Any], raw: Any) -> Any:
        """按 schema 规范化前端传来的值，非法时抛 ValueError。"""
        kind = str(node.get("type") or "")
        if kind == "bool":
            return self._coerce_bool(raw)
        if kind == "int":
            value = self._coerce_number(raw, int)
            self._check_range(key, node, value)
            return value
        if kind == "float":
            value = self._coerce_number(raw, float)
            self._check_range(key, node, value)
            return value
        if kind == "string":
            text = "" if raw is None else str(raw)
            options = node.get("options")
            if isinstance(options, list) and options and text not in options:
                joined = "、".join(str(option) for option in options)
                raise ValueError(f"只能是 {joined} 之一")
            return text
        if kind == "text":
            return "" if raw is None else str(raw)
        if kind == "list":
            return self._coerce_list(raw)
        if kind == "template_list":
            return self._coerce_template_list(key, node, raw)
        if kind == "object":
            return self._coerce_object(key, node, raw)
        if kind == "dict":
            if not isinstance(raw, dict):
                raise ValueError("需要是键值对象")
            return raw
        raise ValueError(f"不支持的配置类型 {kind!r}")

    @staticmethod
    def _coerce_bool(raw: Any) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        text = str(raw).strip().lower()
        if text in {"1", "true", "yes", "on", "开启"}:
            return True
        if text in {"0", "false", "no", "off", "关闭", ""}:
            return False
        raise ValueError("需要是布尔值（true/false）")

    @staticmethod
    def _coerce_number(raw: Any, cast: Any) -> Any:
        if isinstance(raw, bool):
            raise ValueError("需要是数字")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise ValueError("不能为空")
        try:
            return cast(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("需要是数字") from exc

    @staticmethod
    def _check_range(key: str, node: dict[str, Any], value: float) -> None:
        slider = node.get("slider") if isinstance(node.get("slider"), dict) else {}
        low = slider.get("min", node.get("min"))
        high = slider.get("max", node.get("max"))
        if isinstance(low, (int, float)) and value < low:
            raise ValueError(f"不能小于 {low}")
        if isinstance(high, (int, float)) and value > high:
            raise ValueError(f"不能大于 {high}")

    @staticmethod
    def _coerce_list(raw: Any) -> list[str]:
        if isinstance(raw, str):
            raw = re.split(r"[\n,，;；|]+", raw)
        if not isinstance(raw, list):
            raise ValueError("需要是列表")
        items: list[str] = []
        for item in raw:
            if isinstance(item, (list, dict)):
                raise ValueError("列表项只能是字符串")
            text = str(item).strip()
            if text and text not in items:
                items.append(text)
        return items

    def _coerce_object(self, key: str, node: dict[str, Any], raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("需要是键值对象")
        items = node.get("items") if isinstance(node.get("items"), dict) else {}
        result: dict[str, Any] = {}
        for sub_key, sub_node in items.items():
            if not isinstance(sub_node, dict):
                continue
            try:
                result[sub_key] = self._coerce_value(
                    f"{key}.{sub_key}",
                    sub_node,
                    raw.get(sub_key, sub_node.get("default")),
                )
            except ValueError as exc:
                raise ValueError(f"{sub_key}：{exc}") from exc
        return result

    def _coerce_template_list(
        self,
        key: str,
        node: dict[str, Any],
        raw: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise ValueError("需要是列表")
        templates = node.get("templates") if isinstance(node.get("templates"), dict) else {}
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index} 项需要是对象")
            template_key = str(item.get("__template_key") or "")
            template = templates.get(template_key)
            if not isinstance(template, dict):
                known = "、".join(str(name) for name in templates) or "（无可用模板）"
                raise ValueError(
                    f"第 {index} 项的模板 {template_key!r} 不存在，可用模板：{known}",
                )
            sub_items = template.get("items") if isinstance(template.get("items"), dict) else {}
            entry: dict[str, Any] = {"__template_key": template_key}
            for sub_key, sub_node in sub_items.items():
                if not isinstance(sub_node, dict):
                    continue
                try:
                    entry[sub_key] = self._coerce_value(
                        f"{key}[{index}].{sub_key}",
                        sub_node,
                        item.get(sub_key, sub_node.get("default")),
                    )
                except ValueError as exc:
                    raise ValueError(f"第 {index} 项 {sub_key}：{exc}") from exc
            entries.append(entry)
        return entries

    def _semantic_error(self, key: str, value: Any) -> str:
        """拦下运行时会被静默忽略或回退的取值，返回空串表示通过。"""
        if key in TIME_CONFIG_KEYS:
            text = str(value or "").strip()
            if (
                text
                and text.lower() not in DISABLED_TIME_WORDS
                and not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]?\d", text)
            ):
                return "需要是 HH:MM（24 小时制），或填写 off 表示关闭"
        if key == "code_charset" and len(set(str(value or ""))) < 4:
            return "至少需要 4 个不同字符，否则会回退到默认字符集"
        if key == "timezone":
            text = str(value or "").strip()
            if text and not self._tz_available(text):
                return f"时区 {text!r} 不可用，请填 IANA 名称（如 Asia/Shanghai）或留空使用系统时区"
        if key == "review_llm_provider":
            text = str(value or "").strip()
            known = {item["id"] for item in self._provider_options()}
            if text and known and text not in known:
                return f"模型提供商 {text!r} 不存在，可选：{'、'.join(sorted(known))}"
        return ""

    @staticmethod
    def _tz_available(name: str) -> bool:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(name)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ 题库 Pages 后端

    def _bank_public(self) -> dict[str, Any]:
        """题库编辑器需要的全量数据（含停用题目与每题统计）。"""
        stats = self._state.get("question_stats") or {}
        questions: list[dict[str, Any]] = []
        for entry in self._questions(include_disabled=True):
            stat = stats.get(entry["question"]) or {}
            questions.append(
                {
                    # 前端保存时原样回传，缺了就补上（手改过配置文件的也可能没有）
                    "__template_key": QUESTION_BANK_TEMPLATE_KEY,
                    "enabled": bool(entry["enabled"]),
                    "question": entry["question"],
                    "hint": entry["hint"],
                    "answers": list(entry["answers"]),
                    "match_mode": entry["match_mode"],
                    "picks": int(stat.get("picks", 0) or 0),
                    "passes": int(stat.get("passes", 0) or 0),
                    # usable = 真的会被抽中（启用 且 有题干 且 有该题答案或通用答案）
                    "usable": bool(entry["enabled"]) and self._question_usable(entry),
                }
            )
        provider = self._resolve_page_llm_provider()
        return {
            "questions": questions,
            "common_answers": self._common_answers(),
            "match_mode": self._match_mode(),
            "fuzzy_threshold": self._fuzzy_threshold(),
            "match_mode_options": list(MATCH_MODE_CHOICES),
            "review_mode": self._review_mode(),
            "llm_available": provider is not None,
            "llm_provider": "",
            "parse_limit": QUESTION_PARSE_MAX_ITEMS,
            "parse_max_chars": QUESTION_PARSE_MAX_CHARS,
            "meta": self._plugin_meta("questions"),
        }

    async def api_get_questions(self, *args: Any, **kwargs: Any):
        """GET <插件名>/questions：题库（问题库 + 答案库 + 统计）。"""
        data = self._bank_public()
        provider = self._resolve_page_llm_provider()
        if provider is not None:
            try:
                data["llm_provider"] = str(provider.meta().id)
            except Exception:
                data["llm_provider"] = ""
        return json_response({"status": "ok", "data": data})

    def _coerce_bank_payload(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """把前端提交的题库规范化，返回（可保存的值, 错误列表）。"""
        schema = self._load_schema()
        node = schema.get("questions")
        if not isinstance(node, dict):
            return {}, ["无法读取 questions 的配置定义"]
        errors: list[str] = []
        normalized: dict[str, Any] = {}

        raw_questions = payload.get("questions")
        if raw_questions is None:
            raw_questions = []
        if not isinstance(raw_questions, list):
            errors.append("questions 需要是数组")
            raw_questions = []
        elif len(raw_questions) > 200:
            errors.append("题目数量过多（最多 200 条）")
        else:
            prepared = []
            for index, item in enumerate(raw_questions, start=1):
                if not isinstance(item, dict):
                    errors.append(f"第 {index} 条不是对象")
                    continue
                entry = dict(item)
                entry.pop("picks", None)
                entry.pop("passes", None)
                entry.pop("usable", None)
                entry["__template_key"] = str(entry.get("__template_key") or QUESTION_BANK_TEMPLATE_KEY)
                prepared.append(entry)
            try:
                normalized["questions"] = self._coerce_value("questions", node, prepared)
            except ValueError as exc:
                errors.append(str(exc))

        if "common_answers" in payload:
            try:
                normalized["common_answers"] = self._coerce_list(payload.get("common_answers"))
            except ValueError as exc:
                errors.append(f"通用答案库：{exc}")
        return normalized, errors

    async def api_save_questions(self, *args: Any, **kwargs: Any):
        """POST <插件名>/questions：保存问题库与答案库。"""
        payload = await self._read_json_payload(args, default=None)
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        values, errors = self._coerce_bank_payload(payload)
        if errors:
            return error_response("题库校验未通过\n" + "\n".join(f"· {item}" for item in errors))
        if not values:
            return error_response("没有需要保存的内容")
        try:
            await self._persist_config(values)
        except Exception as exc:
            logger.exception("[审核群] 保存题库失败")
            return error_response(f"保存题库失败：{exc}", status_code=500)

        self._warn_config()
        saved = len(values.get("questions") or []) if "questions" in values else 0
        logger.info(
            "[审核群] 题库已通过 WebUI 更新（操作者：%s）：题目 %s 条，通用答案 %s 条。",
            self._request_username(),
            saved,
            len(values.get("common_answers") or []) if "common_answers" in values else "未改动",
        )
        data = self._bank_public()
        data["saved"] = saved
        data["warnings"] = self._semantic_warnings()
        return json_response({"status": "ok", "data": data})

    def _resolve_page_llm_provider(self) -> Any:
        """Pages 场景没有会话上下文：优先用配置的提供商，否则用第一个可用的对话模型。"""
        provider_id = str(self._get("review_llm_provider", "") or "").strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
            if provider is not None and hasattr(provider, "text_chat"):
                return provider
        try:
            providers = self.context.get_all_providers()
        except Exception:
            providers = []
        for provider in providers or []:
            if hasattr(provider, "text_chat"):
                return provider
        return None

    @staticmethod
    def _extract_json_array(raw: str) -> list[Any] | None:
        """从模型回复里抠出第一个 JSON 数组（容忍 ```json 代码块与前后废话）。"""
        text = str(raw or "").strip()
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                candidate = part.strip()
                if candidate.lower().startswith("json"):
                    candidate = candidate[4:].strip()
                if candidate.startswith("["):
                    text = candidate
                    break
        start = text.find("[")
        if start < 0:
            return None
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            return None
        return parsed if isinstance(parsed, list) else None

    async def api_parse_questions(self, *args: Any, **kwargs: Any):
        """POST <插件名>/questions/parse：把一段文本交给大模型，解析成题目与答案草稿。"""
        payload = await self._read_json_payload(args, default=None)
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        text = str(payload.get("text") or "").strip()
        if not text:
            return error_response("请先粘贴要解析的文本")
        if len(text) > QUESTION_PARSE_MAX_CHARS:
            return error_response(
                f"文本太长（{len(text)} 字）：一次最多 {QUESTION_PARSE_MAX_CHARS} 字，请分段解析。",
            )
        provider = self._resolve_page_llm_provider()
        if provider is None:
            return error_response(
                "没有可用的对话模型：请先在 AstrBot 里配置模型，或在插件配置里指定 review_llm_provider。",
            )

        prompt = self._render(
            LLM_PARSE_PROMPT,
            text=text,
            limit=QUESTION_PARSE_MAX_ITEMS,
        )
        timeout = max(30.0, self._float("llm_review_timeout_seconds", 20.0, minimum=1.0))
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=LLM_PARSE_SYSTEM_PROMPT),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return error_response(f"大模型解析超时（>{timeout:.0f} 秒），请缩短文本或稍后重试", status_code=504)
        except Exception as exc:
            logger.exception("[审核群] 题库自动解析失败")
            return error_response(f"调用大模型失败：{exc}", status_code=502)

        raw = str(getattr(response, "completion_text", "") or "")
        items = self._extract_json_array(raw)
        if items is None:
            logger.warning("[审核群] 题库自动解析未能解析出 JSON：%r", raw[:200])
            return error_response(
                "大模型返回的内容不是 JSON 数组，无法自动填入。可以换一段更规整的文本，"
                "或直接把返回内容手动整理成题目。",
                data={"raw": raw[:2000]},
            )

        drafts: list[dict[str, Any]] = []
        for item in items[:QUESTION_PARSE_MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            answers = item.get("answers")
            if isinstance(answers, str):
                answers = re.split(r"[\n,，;；|、]+", answers)
            if not isinstance(answers, list):
                answers = []
            mode = str(item.get("match_mode") or "inherit").strip().lower()
            drafts.append(
                {
                    "question": str(item.get("question") or "").strip(),
                    "answers": [str(answer).strip() for answer in answers if str(answer).strip()],
                    "hint": str(item.get("hint") or "").strip(),
                    "match_mode": mode if mode in MATCH_MODE_CHOICES else "inherit",
                }
            )
        drafts = [item for item in drafts if item["question"]]
        if not drafts:
            return error_response(
                "大模型没有解析出任何题目：确认文本里确实包含问题与答案，或换一段文本再试。",
                data={"raw": raw[:2000]},
            )
        try:
            provider_name = str(provider.meta().id)
        except Exception:
            provider_name = ""
        logger.info("[审核群] 题库自动填入：由 %s 解析出 %s 条题目草稿。", provider_name or "模型", len(drafts))
        return json_response(
            {
                "status": "ok",
                "data": {
                    "drafts": drafts,
                    "provider": provider_name,
                    "raw": raw[:2000],
                    "note": "这些只是草稿，确认/修改后点「保存题库」才会写入配置。",
                },
            },
        )

    # ------------------------------------------------------------------ 平台调用辅助

    def _platform_insts(self) -> list[Any]:
        try:
            return list(self.context.platform_manager.get_insts())
        except Exception:
            return list(getattr(self.context.platform_manager, "platform_insts", []) or [])

    def _aiocqhttp_clients(self) -> list[tuple[str, Any]]:
        clients: list[tuple[str, Any]] = []
        for platform in self._platform_insts():
            try:
                meta = platform.meta()
            except Exception:
                continue
            if str(getattr(meta, "name", "")) != "aiocqhttp":
                continue
            try:
                client = platform.get_client()
            except Exception:
                continue
            if client is not None:
                clients.append((str(getattr(meta, "id", "")), client))
        return clients

    def _clients_for_group(self, group_id: str) -> list[tuple[str, Any]]:
        """优先使用该群最近一次事件所属的平台，其余平台作为兜底。"""
        preferred = str((self._state.get("group_platform") or {}).get(str(group_id)) or "")
        clients = self._aiocqhttp_clients()
        clients.sort(key=lambda item: 0 if item[0] == preferred else 1)
        return clients

    async def _call_action(self, client: Any, action: str, **params: Any) -> Any:
        api = getattr(client, "api", None)
        if api is not None and hasattr(api, "call_action"):
            return await api.call_action(action, **params)
        return await client.call_action(action, **params)

    def _self_ids(self) -> set[str]:
        ids = {str(item) for item in (self._state.get("self_ids") or []) if item}
        ids.update(str(value) for value in self._login_ids.values())
        return ids

    async def _refresh_login_ids(self) -> None:
        for platform_id, client in self._aiocqhttp_clients():
            if platform_id in self._login_ids:
                continue
            try:
                info = await self._call_action(client, "get_login_info")
            except Exception as exc:
                logger.debug("[审核群] 平台 %s 获取登录信息失败：%s", platform_id, exc)
                continue
            if isinstance(info, dict) and info.get("user_id"):
                user_id = str(info["user_id"])
                self._login_ids[platform_id] = user_id
                if user_id not in (self._state.get("self_ids") or []):
                    self._state.setdefault("self_ids", []).append(user_id)
                    self._save_state()

    async def _member_list(self, group_id: str) -> list[dict] | None:
        for platform_id, client in self._clients_for_group(group_id):
            try:
                result = await self._call_action(client, "get_group_member_list", group_id=_as_number(group_id))
            except Exception as exc:
                logger.warning("[审核群] 平台 %s 获取群 %s 成员列表失败：%s", platform_id, group_id, exc)
                continue
            if isinstance(result, list):
                return [member for member in result if isinstance(member, dict)]
            logger.warning("[审核群] 平台 %s 返回的群成员列表格式异常：%s", platform_id, type(result))
        return None

    async def _member_role(self, group_id: str, user_id: str) -> str:
        """查询成员身份（owner/admin/member），带 1 小时内存缓存。"""
        cache_key = self._key(group_id, user_id)
        cached = self._role_cache.get(cache_key)
        now = time.time()
        if cached and now - cached[0] < 3600:
            return cached[1]
        for _, client in self._clients_for_group(group_id):
            try:
                info = await self._call_action(
                    client,
                    "get_group_member_info",
                    group_id=_as_number(group_id),
                    user_id=_as_number(user_id),
                    no_cache=False,
                )
            except Exception as exc:
                logger.debug("[审核群] 查询群 %s 成员 %s 身份失败：%s", group_id, user_id, exc)
                continue
            if isinstance(info, dict):
                role = str(info.get("role") or "member")
                self._role_cache[cache_key] = (now, role)
                return role
        return "member"

    async def _kick_member(self, group_id: str, user_id: str) -> bool:
        if not user_id:
            return False
        reject = self._bool("reject_add_request", False)
        for platform_id, client in self._clients_for_group(group_id):
            try:
                await self._call_action(
                    client,
                    "set_group_kick",
                    group_id=_as_number(group_id),
                    user_id=_as_number(user_id),
                    reject_add_request=reject,
                )
            except Exception as exc:
                logger.debug("[审核群] 平台 %s 移出群 %s 成员 %s 失败：%s", platform_id, group_id, user_id, exc)
                continue
            logger.info("[审核群] 已移出群 %s 的成员 %s。", group_id, user_id)
            return True
        logger.warning("[审核群] 移出群 %s 成员 %s 失败，请确认机器人是该群管理员。", group_id, user_id)
        return False

    # ------------------------------------------------------------------ 发送辅助

    async def _send_event_chain(self, event: AstrMessageEvent, components: list[Any]) -> bool:
        try:
            await event.send(event.chain_result(components))
            return True
        except Exception:
            logger.exception("[审核群] 发送消息失败（事件回复）")
            return False

    async def _send_umo(self, umo: str, text: str) -> bool:
        try:
            return await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:
            logger.warning("[审核群] 向 %s 发送消息失败：%s", umo, exc)
            return False

    async def _send_to_group(self, group_id: str, components: list[Any] | None = None, text: str = "") -> bool:
        chain = MessageChain()
        if components:
            chain.chain.extend(components)
        if text:
            chain.chain.append(Plain(text))
        if not chain.chain:
            return False
        for platform_id in self._candidate_platform_ids(group_id):
            umo = f"{platform_id}:{GROUP_MESSAGE_TYPE}:{group_id}"
            try:
                if await self.context.send_message(umo, chain):
                    return True
            except Exception as exc:
                logger.warning("[审核群] 向群 %s 发送消息失败（%s）：%s", group_id, umo, exc)
        logger.warning("[审核群] 未找到可向群 %s 发送消息的平台适配器。", group_id)
        return False

    def _private_send_channel(self) -> str:
        channel = str(self._get("private_send_channel", "auto") or "auto").strip().lower()
        return channel if channel in PRIVATE_SEND_CHANNELS else "auto"

    async def _send_temp_session(self, group_id: str, user_id: str, text: str, platform_id: str = "") -> bool:
        """走 OneBot 的群临时会话（send_private_msg + group_id）：成员无需加好友即可收到。

        临时会话需要「机器人与该成员在同一个群」，且多数实现要求成员近期在群里说过话；
        我们的流程正是"成员在群里答完题就发码"，所以通常可用。
        """
        if not group_id or not user_id or not text:
            return False
        clients = self._clients_for_group(group_id)
        if platform_id:
            # 事件来源平台优先，其它账号作为兜底
            clients.sort(key=lambda item: 0 if item[0] == platform_id else 1)
        for candidate, client in clients:
            try:
                await self._call_action(
                    client,
                    "send_private_msg",
                    user_id=_as_number(user_id),
                    group_id=_as_number(group_id),
                    message=text,
                )
            except Exception as exc:
                logger.debug(
                    "[审核群] 平台 %s 用群 %s 的临时会话给 %s 发送失败：%s",
                    candidate,
                    group_id,
                    user_id,
                    exc,
                )
                continue
            logger.info("[审核群] 已通过群 %s 的临时会话向 %s 发送消息。", group_id, user_id)
            return True
        return False

    async def _send_private(
        self,
        platform_id: str,
        user_id: str,
        text: str,
        group_id: str = "",
    ) -> bool:
        """私聊发送。默认先走群临时会话（无需加好友），失败再退回好友私聊。"""
        channel = self._private_send_channel()
        if channel in {"auto", "temp_session"} and group_id:
            if await self._send_temp_session(group_id, user_id, text, platform_id=str(platform_id or "")):
                return True
            if channel == "temp_session":
                logger.warning(
                    "[审核群] 群 %s 的临时会话发送失败（用户 %s）："
                    "成员需要在群里发过言，且机器人与他在同一个群。",
                    group_id,
                    user_id,
                )
                return False

        candidates = [str(platform_id)] if platform_id else []
        candidates.extend(pid for pid, _ in self._aiocqhttp_clients() if pid not in candidates)
        for candidate in candidates:
            umo = f"{candidate}:{FRIEND_MESSAGE_TYPE}:{user_id}"
            try:
                if await self.context.send_message(umo, MessageChain().message(text)):
                    return True
            except Exception as exc:
                logger.debug("[审核群] 好友私聊 %s 失败（%s）：%s", user_id, umo, exc)
        logger.info(
            "[审核群] 私聊 %s 失败（临时会话与好友私聊都没成功），请确认双方在同一群且机器人已加对方为好友。",
            user_id,
        )
        return False

    def _candidate_platform_ids(self, group_id: str) -> list[str]:
        preferred = str((self._state.get("group_platform") or {}).get(str(group_id)) or "")
        ids = [platform_id for platform_id, _ in self._aiocqhttp_clients()]
        ordered = [platform_id for platform_id in ids if platform_id == preferred]
        ordered.extend(platform_id for platform_id in ids if platform_id != preferred)
        return list(dict.fromkeys(ordered))

    # ------------------------------------------------------------------ 状态读写

    def _key(self, group_id: str, user_id: str) -> str:
        return f"{group_id}:{user_id}"

    def _diag_bump(self, key: str) -> None:
        counters = self._diag.setdefault("counters", {})
        counters[key] = int(counters.get(key, 0)) + 1

    def _diag_set_reason(self, reason: str) -> None:
        self._diag["last_reason"] = str(reason)

    def _diag_fmt(self, record: Any) -> str:
        if not isinstance(record, dict):
            return "（无）"
        at = self._fmt_ts(record.get("at"))
        if "text" in record:
            return f"{at} ｜ 群 {record.get('group_id')} ｜ 用户 {record.get('user_id')} ｜ 内容 {record.get('text')!r}"
        if "scene" in record:
            return f"{at} ｜ {record.get('scene')} ｜ 群 {record.get('group_id')} ｜ 用户 {record.get('user_id')}"
        return f"{at} ｜ {record.get('type')} ｜ 群 {record.get('group_id')} ｜ 用户 {record.get('user_id')}"

    def _diag_verdict(self, counters: dict[str, int]) -> str:
        """根据事件计数判断「为什么没给新人发消息」。"""
        verdict = self._diag_main_verdict(counters)
        failures = int(counters.get("send_fail", 0))
        if failures:
            # 有成功也有失败时，主结论会偏乐观，这里必须把失败次数单独点出来
            verdict += f"\n另有 {failures} 次发送失败：检查机器人是否被禁言/被风控，或协议端是否异常。"
        return verdict

    def _diag_main_verdict(self, counters: dict[str, int]) -> str:
        if not self._review_groups():
            return "未配置 review_groups（审核群群号），插件不会做任何事。"
        if self._pick_question() is None:
            return "questions 里没有可用题目（问题与答案都要填），新人入群不会收到问题。"
        if counters.get("notices", 0) == 0 and counters.get("messages", 0) == 0:
            return (
                "插件还没有收到过任何群消息或群通知：插件可能未加载、被会话/白名单限制，"
                "或协议端没有连上。先让人在审核群里发一句话，再看「群消息」计数是否变化。"
            )
        if counters.get("notices", 0) == 0:
            return (
                "能收到群消息、但收不到任何群通知：协议端没有上报群事件（group_increase）。"
                "请检查协议端（NapCat/Lagrange）的事件上报配置。"
            )
        if counters.get("increase", 0) == 0 and counters.get("other_group", 0) > 0:
            return "收到的群通知都不属于已配置的审核群：请核对 review_groups 里的群号。"
        if counters.get("increase", 0) == 0:
            return "收到的通知里没有 group_increase（入群事件）：确认测试时确实有新成员加入本群。"
        if counters.get("asked", 0) == 0 and counters.get("exempt", 0) > 0:
            return "入群的人都在 admin_ids / exempt_user_ids 里，被有意跳过了。"
        if counters.get("asked", 0) == 0 and counters.get("send_fail", 0) > 0:
            return "收到入群通知但发送失败：检查机器人是否被禁言、被限制或协议端异常。"
        if counters.get("asked", 0) == 0 and counters.get("no_question", 0) > 0:
            return "收到入群通知，但当时没有可用的审核题目，已跳过。"
        if counters.get("asked", 0) > 0:
            return (
                "插件已成功发送过审核问题（见「最近提问」）。若新人没看到，"
                "注意机器人加入本群之前就已经在群里的人不会有入群通知，"
                "让他发一句话即可触发补发问题（auto_enroll_on_speak）。"
            )
        return "暂未发现明确问题，请把这份诊断连同插件日志一起提供。"

    def _new_record(self, group_id: str, user_id: str, question: dict[str, Any], platform_id: str) -> dict[str, Any]:
        return {
            "user_id": str(user_id),
            "group_id": str(group_id),
            "platform_id": str(platform_id or ""),
            "question": str(question.get("question") or ""),
            "hint": str(question.get("hint") or ""),
            "answers": [str(answer) for answer in (question.get("answers") or [])],
            # 抽题时就把匹配方式固化下来，之后改配置不会影响正在进行的审核
            "match_mode": self._resolve_match_mode(question.get("match_mode") or "inherit"),
            "attempts": 0,
            "asked_at": time.time(),
        }

    def _bump_question_stat(self, question: str, key: str) -> None:
        """按题面文本累计抽中/通过次数（题面被改写则重新计数）。"""
        question = str(question or "").strip()
        if not question:
            return
        stats = self._state.setdefault("question_stats", {})
        entry = stats.setdefault(question, {"picks": 0, "passes": 0})
        entry[key] = int(entry.get(key, 0)) + 1

    def _render(self, template: str, **kwargs: Any) -> str:
        """用 str.replace 渲染模板，模板里出现无关花括号也不会报错。"""
        text = template or ""
        for key, value in kwargs.items():
            text = text.replace("{" + key + "}", str(value))
        return text

    def _remember_event(self, event: AstrMessageEvent, group_id: str) -> None:
        changed = False
        self_id = str(event.get_self_id() or "")
        if self_id and self_id not in (self._state.get("self_ids") or []):
            self._state.setdefault("self_ids", []).append(self_id)
            changed = True
        platform_id = str(event.get_platform_id() or "")
        recorded = str((self._state.get("group_platform") or {}).get(str(group_id)) or "")
        if platform_id and str(group_id) and recorded != platform_id:
            self._state.setdefault("group_platform", {})[str(group_id)] = platform_id
            changed = True
        if changed:
            self._save_state()

    def _load_state(self) -> dict[str, Any]:
        state = self._empty_state()
        path = self._state_path
        if path is None or not path.exists():
            return state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[审核群] 读取状态文件失败，将使用空状态：%s", path)
            return state
        if not isinstance(data, dict):
            return state
        for key, default in state.items():
            value = data.get(key, default)
            if isinstance(default, dict) and not isinstance(value, dict):
                value = default
            if isinstance(default, list) and not isinstance(value, list):
                value = default
            state[key] = value
        return state

    def _save_state(self) -> None:
        path = self._state_path
        if path is None:
            return
        try:
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        except Exception:
            logger.exception("[审核群] 保存状态文件失败：%s", path)
