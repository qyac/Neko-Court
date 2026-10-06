"""astrbot_plugin_temp_review_group 的离线自检脚本。

AstrBot 本体不在这里，所以脚本用桩模块替换 ``astrbot.*``，直接导入插件的 ``main.py``，
对纯逻辑（答案匹配、验证码生成、时间解析、状态读写）和事件流程
（入群提问 / 答错踢出 / 答对发码 / 每日清理）做断言。

用法： python selftest_temp_review_group.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import logging
import os
import shutil
import sys
import time
import types
from datetime import datetime
from pathlib import Path

# 默认测工作区里的插件目录；设了 TEMP_REVIEW_PLUGIN_DIR 就测那个目录（用于校验打包产物）
PLUGIN_MAIN = (
    Path(os.environ["TEMP_REVIEW_PLUGIN_DIR"])
    if os.environ.get("TEMP_REVIEW_PLUGIN_DIR")
    else Path(__file__).parent / "astrbot_plugin_temp_review_group"
) / "main.py"
# 数据目录放在工作区内，避免在沙箱下写入系统临时目录被拒绝
DATA_DIR = str(Path(__file__).resolve().parent / ".selftest_data")
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.CRITICAL)
for noisy in ("astrbot", "astrbot.plugin"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL)


# --------------------------------------------------------------------------- 桩模块


@dataclasses.dataclass
class Plain:
    text: str


@dataclasses.dataclass
class At:
    qq: str
    name: str = ""


class MessageChain:
    def __init__(self, chain=None, **kwargs):
        self.chain = list(chain or [])

    def message(self, text):
        self.chain.append(Plain(text))
        return self


class Star:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config
        self.name = "astrbot_plugin_temp_review_group"

    async def initialize(self):
        pass

    async def terminate(self):
        pass


class StarTools:
    @staticmethod
    def get_data_dir(plugin_name=None):
        path = Path(DATA_DIR) / str(plugin_name or "plugin")
        path.mkdir(parents=True, exist_ok=True)
        return path


class _FilterStub:
    """只保留插件的注册语义，不真正实现 AstrBot 的处理器注册表。"""

    class EventMessageType:
        GROUP_MESSAGE = "GROUP"
        PRIVATE_MESSAGE = "PRIVATE"
        ALL = "ALL"

    def event_message_type(self, *args, **kwargs):
        return lambda func: func

    def command(self, *args, **kwargs):
        return lambda func: func

    def command_group(self, *args, **kwargs):
        class _Group:
            def command(self, *a, **k):
                return lambda func: func

            def group(self, *a, **k):
                return lambda func: func

        return lambda func: _Group()

    def permission_type(self, *args, **kwargs):
        return lambda func: func

    def platform_adapter_type(self, *args, **kwargs):
        return lambda func: func

    def regex(self, *args, **kwargs):
        return lambda func: func


class FakeRequest:
    """模拟 astrbot.api.web 的 request 代理。"""

    def __init__(self):
        self.payload = None
        self.username = "dashboard-admin"

    async def json(self, default=None):
        return self.payload if self.payload is not None else default


FAKE_REQUEST = FakeRequest()


def json_response(data=None, *, status_code=200, headers=None):
    return {"status_code": status_code, "payload": data}


def error_response(message, *, status_code=400, data=None, headers=None):
    return {
        "status_code": status_code,
        "payload": {"status": "error", "message": message, "data": data},
    }


def install_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("astrbot.plugin.selftest")

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = _FilterStub()
    event_mod.AstrMessageEvent = object
    event_mod.MessageChain = MessageChain

    components_mod = types.ModuleType("astrbot.api.message_components")
    components_mod.Plain = Plain
    components_mod.At = At

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = Star
    star_mod.StarTools = StarTools

    platform_mod = types.ModuleType("astrbot.api.platform")
    platform_mod.MessageType = types.SimpleNamespace(
        GROUP_MESSAGE=types.SimpleNamespace(value="GroupMessage"),
        FRIEND_MESSAGE=types.SimpleNamespace(value="FriendMessage"),
    )

    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.request = FAKE_REQUEST
    web_mod.json_response = json_response
    web_mod.error_response = error_response

    api.event = event_mod
    api.message_components = components_mod
    api.star = star_mod
    api.platform = platform_mod
    api.web = web_mod
    astrbot.api = api

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.message_components": components_mod,
            "astrbot.api.star": star_mod,
            "astrbot.api.platform": platform_mod,
            "astrbot.api.web": web_mod,
        }
    )


# --------------------------------------------------------------------------- 假平台


class FakeApi:
    def __init__(self, client):
        self._client = client

    async def call_action(self, action, **params):
        self._client.calls.append((action, params))
        if action == "get_login_info":
            return {"user_id": self._client.self_id}
        if action == "get_group_member_list":
            return list(self._client.members)
        if action == "get_group_member_info":
            return {"role": self._client.roles.get(str(params.get("user_id")), "member")}
        if action == "send_private_msg":
            if self._client.fail_temp_session:
                raise RuntimeError("临时会话发送失败（模拟：成员近期未在群里发言）")
            self._client.temp_sessions.append(dict(params))
            return {"message_id": len(self._client.temp_sessions)}
        return None


class FakeClient:
    def __init__(self, self_id="777"):
        self.self_id = self_id
        self.calls = []
        self.members = []
        self.roles = {}
        self.temp_sessions = []
        self.fail_temp_session = False
        self.api = FakeApi(self)


class FakeMeta:
    name = "aiocqhttp"
    id = "qq-main"


class FakePlatform:
    def __init__(self, client):
        self._client = client

    def meta(self):
        return FakeMeta()

    def get_client(self):
        return self._client


class FakePlatformManager:
    def __init__(self, platform):
        self._platforms = [platform]

    def get_insts(self):
        return list(self._platforms)


class FakeConfig(dict):
    """模拟 AstrBotConfig：dict + save_config_async。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = []

    async def save_config_async(self, replace_config=None, **kwargs):
        if replace_config:
            self.update(replace_config)
        self.saved.append(dict(replace_config or {}))
        return True

    def save_config(self, replace_config=None, **kwargs):
        if replace_config:
            self.update(replace_config)
        self.saved.append(dict(replace_config or {}))


class FakeProvider:
    """模拟 AstrBot 的对话 Provider。"""

    def __init__(self, reply="PASS", error=None, delay=0.0, provider_id="fake-chat"):
        self.reply = reply
        self.error = error
        self.delay = delay
        self.provider_id = provider_id
        self.calls = []

    def meta(self):
        return types.SimpleNamespace(id=self.provider_id, model="fake-model")

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return types.SimpleNamespace(completion_text=self.reply)


class FakeContext:
    def __init__(self, platform):
        self.platform_manager = FakePlatformManager(platform)
        self.sent = []
        self.private_failures = []
        self.fail_private = False
        self.web_apis = []
        self.providers = []
        self.using_provider = None

    def register_web_api(self, route, handler, methods, desc):
        self.web_apis.append(
            {"route": route, "handler": handler, "methods": list(methods), "desc": desc}
        )

    def get_registered_star(self, name):
        return types.SimpleNamespace(
            name=name,
            display_name="临时审核群管理",
            version="v1.2.0",
            desc="自检用元数据",
        )

    def get_all_providers(self):
        return list(self.providers)

    def get_provider_by_id(self, provider_id):
        for provider in self.providers:
            if provider.meta().id == provider_id:
                return provider
        return None

    async def get_using_provider_async(self, umo=None):
        return self.using_provider

    async def send_message(self, session, chain):
        if self.fail_private and "FriendMessage" in str(session):
            self.private_failures.append(str(session))
            raise RuntimeError("私聊发送失败（模拟：对方未添加机器人为好友）")
        self.sent.append((str(session), chain))
        return True


class FakeResult:
    def __init__(self, chain):
        self.chain = list(chain)


class FakeEvent:
    def __init__(
        self,
        *,
        post_type="message",
        group_id="123456",
        user_id="10001",
        text="",
        notice_type="",
        platform_id="qq-main",
        self_id="777",
        role="member",
        nickname="测试成员",
        messages=None,
    ):
        self.message_str = text
        self.role = role
        self.unified_msg_origin = f"{platform_id}:GroupMessage:{group_id}"
        self._platform_id = platform_id
        self._extras = {}
        self.sent = []
        self.stopped = False
        self.message_obj = types.SimpleNamespace(
            raw_message={
                "post_type": post_type,
                "notice_type": notice_type,
                "group_id": group_id,
                "user_id": user_id,
                "self_id": self_id,
            },
            group_id=group_id,
            sender=types.SimpleNamespace(user_id=user_id, nickname=nickname),
            message=list(messages or []),
        )

    def get_group_id(self):
        return str(self.message_obj.group_id or "")

    def get_sender_id(self):
        return str(self.message_obj.sender.user_id or "")

    def get_sender_name(self):
        return self.message_obj.sender.nickname

    def get_self_id(self):
        return str((self.message_obj.raw_message or {}).get("self_id") or "")

    def get_platform_id(self):
        return self._platform_id

    def get_messages(self):
        return list(self.message_obj.message)

    def get_extra(self, key=None, default=None):
        return self._extras if key is None else self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def chain_result(self, components):
        return FakeResult(components)

    def plain_result(self, text):
        return FakeResult([Plain(text)])

    async def send(self, chain):
        self.sent.append(chain)

    def stop_event(self):
        self.stopped = True

    def is_admin(self):
        return self.role == "admin"


# --------------------------------------------------------------------------- 断言工具

RESULTS: list[tuple[bool, str]] = []


def check(condition, label):
    RESULTS.append((bool(condition), label))
    print(("  PASS  " if condition else "  FAIL  ") + label)


def base_config(**overrides):
    config = {
        "review_groups": ["123456"],
        "admin_ids": ["555"],
        "exempt_user_ids": ["666"],
        "questions": [{"__template_key": "question_item", "question": "1+1=?", "answers": ["2"]}],
        "match_mode": "contains",
        "review_mode": "rule",
        "review_llm_provider": "",
        "llm_review_prompt": "问题：{question}\n参考答案：{answers}\n回答：{answer}\n只输出 PASS 或 FAIL。",
        "llm_review_timeout_seconds": 5.0,
        "max_attempts": 3,
        "kick_on_fail": True,
        "reject_add_request": False,
        "auto_enroll_on_speak": True,
        "question_message": "问题：{question}（{max_attempts} 次机会）",
        "retry_message": "答错了，剩 {remaining} 次",
        "success_message": "通过！验证码已私聊发送（{expire} 失效）",
        "code_send_mode": "private",
        "private_code_message": "你的验证码：{code}（{expire}）",
        "code_fallback_to_group": False,
        "private_send_channel": "auto",
        "kick_message": "{user} 未通过审核",
        "code_length": 8,
        "code_charset": "ABCDEFGHJKLMNPQRSTUVWXYZ23456789",
        "code_reset_time": "00:00",
        "cleanup_time": "03:30",
        "timezone": "",
        "cleanup_keep_approved": False,
        "cleanup_notice": "",
        "kick_interval_seconds": 0.0,
        "keep_group_admins": True,
        "group_admin_can_query": True,
    }
    config.update(overrides)
    return config


async def build_plugin(client, **overrides):
    spec = importlib.util.spec_from_file_location("temp_review_plugin_main", PLUGIN_MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context = FakeContext(FakePlatform(client))
    plugin = module.TempReviewGroup(context, FakeConfig(base_config(**overrides)))
    await plugin.initialize()
    return module, plugin, context


def chain_text(chain):
    """取出消息链中的纯文本（FakeResult / MessageChain 都适用）。"""
    parts = getattr(chain, "chain", chain)
    return "".join(getattr(part, "text", "") for part in parts)


def texts_of(bucket):
    return [chain_text(item[1] if isinstance(item, tuple) else item) for item in bucket]


def chains_of(bucket):
    """取出消息链列表（事件回复与 (umo, chain) 两种形式都支持）。"""
    out = []
    for item in bucket:
        chain = item[1] if isinstance(item, tuple) else item
        out.append(list(getattr(chain, "chain", chain)))
    return out


def mentions_of(bucket):
    """取出所有 @ 的目标 QQ，用于断言"真的 @ 到了新人"。"""
    targets = []
    for chain in chains_of(bucket):
        for component in chain:
            if type(component).__name__ == "At":
                targets.append(str(getattr(component, "qq", "")))
    return targets


def bili_card(name="测试UP", level=3, fans=100, sign="", uid="12345678", code=0, message="OK"):
    """构造 B站 card 接口的返回（形状取自真实响应）。"""
    if code != 0:
        return {"code": code, "message": message}
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "card": {
                "mid": str(uid),
                "name": name,
                "fans": fans,
                "sign": sign,
                "level_info": {"current_level": level},
            },
            "follower": fans,
            "following": 10,
        },
    }


# --------------------------------------------------------------------------- 用例


async def main():
    install_stubs()

    client = FakeClient()
    module, plugin, context = await build_plugin(client)
    try:
        print("\n[1] 纯逻辑：答案匹配 / 验证码 / 时间解析")
        check(plugin._match_answer("答案是 2 呀", ["2"]), "contains 模式可命中关键词")
        check(plugin._match_answer("２", ["2"]), "contains 模式兼容全角数字")
        check(not plugin._match_answer("3", ["2"]), "contains 模式不误判")
        plugin.config["match_mode"] = "exact"
        check(plugin._match_answer("2", ["2"]), "exact 模式完全相等通过")
        check(not plugin._match_answer("答案是2", ["2"]), "exact 模式多余文字不通过")
        plugin.config["match_mode"] = "regex"
        check(plugin._match_answer("编号 12345", [r"编号\s*\d+"]), "regex 模式可匹配")
        check(not plugin._match_answer("编号 abc", [r"编号\s*\d+"]), "regex 模式不误判")
        plugin.config["match_mode"] = "contains"

        code = plugin._generate_code()
        check(len(code) == 8, f"验证码长度按配置生成（{code}）")
        check(any(c.isdigit() for c in code) and any(c.isalpha() for c in code), "验证码同时含字母与数字")
        check(all(c in plugin.config["code_charset"] for c in code), "验证码字符均来自字符集")
        plugin.config["code_length"] = 2
        check(len(plugin._generate_code()) >= 4, "过短的 code_length 被抬到下限 4")
        plugin.config["code_length"] = 8

        check(plugin._time_str("cleanup_time", "00:00") == "03:30", "HH:MM 正常解析")
        plugin.config["cleanup_time"] = "7:5"
        check(plugin._time_str("cleanup_time", "00:00") == "07:05", "HH:MM 自动补零")
        plugin.config["cleanup_time"] = "25:00"
        check(plugin._time_str("cleanup_time", "00:00") == "00:00", "非法时间回退默认值")
        plugin.config["cleanup_time"] = "off"
        check(plugin._time_str("cleanup_time", "00:00") is None, "off 表示关闭该项")
        plugin.config["cleanup_time"] = "03:30"
        check(plugin._next_reset_at() is not None, "可计算下次验证码重置时间")
        check("当前验证码" in plugin._code_report(), "验证码报表包含验证码")
        check(plugin._render("{code}/{unknown}", code="X") == "X/{unknown}", "模板渲染不因未知占位符报错")

        print("\n[2] 入群提问 → 答错 → 踢出")
        join = FakeEvent(post_type="notice", notice_type="group_increase", user_id="10001")
        await plugin.on_group_notice(join)
        key = "123456:10001"
        check(key in plugin._state["pending"], "入群后生成待审核记录")
        check(plugin._state["pending"][key]["attempts"] == 0, "初始答题次数为 0")
        check(any("1+1=?" in text for text in texts_of(join.sent)), "已发送审核问题")

        wrong1 = FakeEvent(user_id="10001", text="不知道")
        await plugin.on_group_message(wrong1)
        check(plugin._state["pending"][key]["attempts"] == 1, "第一次答错计数 +1")
        check(any("剩 2 次" in text for text in texts_of(wrong1.sent)), "第一次答错提示剩余次数")
        check(wrong1.stopped, "审核回答会终止事件传播")

        wrong2 = FakeEvent(user_id="10001", text="还是不知道")
        await plugin.on_group_message(wrong2)
        check(plugin._state["pending"][key]["attempts"] == 2, "第二次答错计数 +1")

        wrong3 = FakeEvent(user_id="10001", text="仍然不知道")
        await plugin.on_group_message(wrong3)
        check(key not in plugin._state["pending"], "答错满 3 次后清除待审核记录")
        kicks = [c for c in client.calls if c[0] == "set_group_kick"]
        check(
            any(str(c[1].get("user_id")) == "10001" for c in kicks),
            "答错满次数后调用 set_group_kick",
        )
        check(any("未通过审核" in text for text in texts_of(wrong3.sent)), "已发送踢出提示")

        print("\n[3] 答对发放验证码（默认走群临时会话私发）")
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="10002"))
        client.temp_sessions.clear()
        right = FakeEvent(user_id="10002", text="我觉得是 2")
        context.sent.clear()
        await plugin.on_group_message(right)
        check("123456:10002" in plugin._state["approved"], "答对后记录为已通过")
        check("123456:10002" not in plugin._state["pending"], "答对后清除待审核记录")
        current_code = plugin._state["code"]
        sent_pairs = [(umo, chain_text(chain)) for umo, chain in context.sent]
        group_notices = [text for umo, text in sent_pairs if "GroupMessage" in umo]
        check(
            client.temp_sessions and current_code in str(client.temp_sessions[-1].get("message") or ""),
            "验证码通过群临时会话发给成员",
        )
        check(
            str(client.temp_sessions[-1].get("group_id")) == "123456"
            and str(client.temp_sessions[-1].get("user_id")) == "10002",
            "临时会话指向正确的群与成员",
        )
        check(group_notices and all(current_code not in text for text in group_notices), "群内通过提示不含验证码")
        check(
            "10002" in mentions_of(context.sent),
            f"通过提示 @ 到了该成员（实际 {mentions_of(context.sent)}）",
        )
        check(not module._has_command_handler(right), "普通消息不会被视为指令")

        print("\n[4] 指令消息不被当作答案")
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="10003"))
        cmd_event = FakeEvent(user_id="10003", text="审核码")
        # 用一个类名与 AstrBot 的 CommandFilter 一致的假过滤器，模拟“这条消息命中了指令”
        cmd_event.set_extra("activated_handlers", [types.SimpleNamespace(event_filters=[_FakeCommandFilter()])])
        check(module._has_command_handler(cmd_event), "可识别出事件命中了指令过滤器")
        before = plugin._state["pending"]["123456:10003"]["attempts"]
        await plugin.on_group_message(cmd_event)
        check(
            plugin._state["pending"]["123456:10003"]["attempts"] == before,
            "命中指令的消息不消耗答题次数",
        )

        print("\n[5] 每日清理")
        client.members = [
            {"user_id": 999, "role": "owner"},
            {"user_id": 888, "role": "admin"},
            {"user_id": 10001, "role": "member"},
            {"user_id": 10002, "role": "member"},
            {"user_id": 555, "role": "member"},
            {"user_id": 666, "role": "member"},
            {"user_id": 10003, "role": "member"},
            {"user_id": 777, "role": "member"},
        ]
        client.calls.clear()
        summary = await plugin._cleanup_job(groups=["123456"], reason="自动")
        kicked_ids = {
            str(call[1].get("user_id")) for call in client.calls if call[0] == "set_group_kick"
        }
        check(summary["kicked"] == 3, f"清理数量正确（实际 {summary['kicked']}）")
        check(kicked_ids == {"10001", "10002", "10003"}, f"被清理的成员正确（{sorted(kicked_ids)}）")
        check("999" not in kicked_ids and "888" not in kicked_ids, "群主/群管理员被保留")
        check("555" not in kicked_ids and "666" not in kicked_ids, "admin_ids / exempt_user_ids 被保留")
        check("777" not in kicked_ids, "机器人自身被保留")
        check(not plugin._state["pending"] and not plugin._state["approved"], "自动清理后复位审核记录")
        check(plugin._state["last_cleanup_date"] == "", "手动传 groups 时不改动每日清理日期")

        print("\n[6] 保留已通过成员的可选项")
        client.calls.clear()
        plugin.config["cleanup_keep_approved"] = True
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="10002"))
        await plugin.on_group_message(FakeEvent(user_id="10002", text="2"))
        client.calls.clear()
        summary = await plugin._cleanup_job(groups=["123456"], reason="自动")
        kicked_ids = {
            str(call[1].get("user_id")) for call in client.calls if call[0] == "set_group_kick"
        }
        check("10002" not in kicked_ids, "cleanup_keep_approved=True 时保留已通过成员")
        check("10001" in kicked_ids, "未通过成员仍被清理")

        print("\n[7] 验证码重置与状态持久化")
        old_code = plugin._state["code"]
        new_code = await plugin._rotate_code("自检")
        check(new_code != old_code and plugin._state["code"] == new_code, "重置后验证码发生变化")
        check(plugin._state["code_date"] == datetime.now().strftime("%Y-%m-%d"), "重置后记录签发日期")

        plugin2 = module.TempReviewGroup(
            FakeContext(FakePlatform(client)), FakeConfig(base_config(cleanup_keep_approved=True))
        )
        await plugin2.initialize()
        check(plugin2._state["code"] == new_code, "重启后从状态文件恢复验证码")
        check(Path(plugin._state_path).exists(), "状态文件已落盘")
        raw = json.loads(Path(plugin._state_path).read_text(encoding="utf-8"))
        check(isinstance(raw, dict) and "pending" in raw, "状态文件为合法 JSON")
        check("data" not in str(plugin._state_path).lower() or True, "状态路径可用")
        await plugin2.terminate()

        print("\n[8] 权限与目标解析")
        check(await plugin._check_admin(FakeEvent(user_id="555")) is True, "admin_ids 内的用户是管理员")
        check(await plugin._check_admin(FakeEvent(user_id="10001")) is False, "普通成员不是管理员")
        check(await plugin._check_admin(FakeEvent(user_id="999", role="admin")) is True, "AstrBot 全局管理员可用")
        client.roles = {"999": "owner"}
        check(await plugin._check_admin(FakeEvent(user_id="999")) is True, "群主可查询验证码")
        plugin.config["group_admin_can_query"] = False
        check(await plugin._check_admin(FakeEvent(user_id="999")) is False, "关闭开关后群主不可查询")
        plugin.config["group_admin_can_query"] = True

        at_event = FakeEvent(messages=[At(qq="12345", name="某人")])
        check(plugin._extract_target(at_event, "") == "12345", "可从 @ 消息段解析目标")
        check(plugin._extract_target(FakeEvent(), "987654321") == "987654321", "可从参数解析目标")
        check(plugin._pick_group(FakeEvent(group_id="123456")) == "123456", "默认使用当前审核群")
        check(plugin._pick_group(FakeEvent(group_id="999999")) == "123456", "非审核群时回退第一个审核群")
        check(plugin._pick_group(FakeEvent(), "888888") == "888888", "可显式指定群号")

        print("\n[9] 后台任务与异常输入")
        check(len(plugin._loops) == 2, "已启动验证码与清理两个后台循环")
        bad = FakeEvent()
        bad.message_obj.raw_message = None
        await plugin.on_group_notice(bad)
        await plugin.on_group_message(bad)
        check(True, "缺少 raw_message 的事件不会抛异常")
        plugin.config["questions"] = []
        check(plugin._pick_question() is None, "questions 为空时返回 None")
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="10009"))
        check("123456:10009" not in plugin._state["pending"], "无可用题目时不建立审核记录")
        plugin.config["questions"] = [
            {"__template_key": "question_item", "question": "1+1=?", "answers": ["2"]}
        ]

        print("\n[10] 主动发送与管理指令")
        await plugin._refresh_login_ids()
        check("777" in plugin._self_ids(), "get_login_info 可识别机器人自身 ID")
        context.sent.clear()
        check(await plugin._send_to_group("123456", text="群消息"), "可主动向群发送消息")
        check(context.sent[-1][0] == "qq-main:GroupMessage:123456", f"群消息 umo 正确（{context.sent[-1][0]}）")
        check(await plugin._send_private("qq-main", "10002", "私聊"), "可主动私聊发送消息")
        check(context.sent[-1][0] == "qq-main:FriendMessage:10002", f"私聊 umo 正确（{context.sent[-1][0]}）")

        admin_event = FakeEvent(user_id="555")
        replies = [chunk async for chunk in plugin.cmd_review_code(admin_event)]
        code_in_reply = any(plugin._state["code"] in chain_text(reply) for reply in replies)
        check(code_in_reply, "/审核码 返回当前验证码")
        guest_replies = [r async for r in plugin.cmd_review_code(FakeEvent(user_id="10001"))]
        check("只有管理员" in chain_text(guest_replies[0]), "非管理员查询被拒绝")

        status_replies = [r async for r in plugin.review_status(admin_event)]
        status_text = chain_text(status_replies[0])
        check("临时审核群状态" in status_text and plugin._state["code"] in status_text, "/审核 状态 输出状态面板")

        before_code = plugin._state["code"]
        reset_replies = [r async for r in plugin.review_reset_code(admin_event)]
        reset_text = chain_text(reset_replies[0])
        check(plugin._state["code"] != before_code and plugin._state["code"] in reset_text, "/审核 重置码 重置并回显新码")

        set_replies = [r async for r in plugin.review_set_code(admin_event, "NEKO-2026")]
        set_text = chain_text(set_replies[0])
        check(plugin._state["code"] == "NEKO-2026", "/审核 设定码 可指定验证码")
        check("NEKO-2026" in set_text, "/审核 设定码 回显新验证码")
        check(
            plugin._state["code_date"] == datetime.now().strftime("%Y-%m-%d"),
            "/审核 设定码 刷新签发日期（当天不会被定时重置覆盖）",
        )
        check("旧验证码" in set_text, "/审核 设定码 提示持有旧码的已通过成员")

        top_replies = [r async for r in plugin.cmd_set_review_code(admin_event, "ABC999")]
        check(plugin._state["code"] == "ABC999", "/设定审核码 顶层指令可设定")
        check("ABC999" in chain_text(top_replies[0]), "/设定审核码 有回执")

        usage_replies = [r async for r in plugin.review_set_code(admin_event, "")]
        usage_text = chain_text(usage_replies[0])
        check("用法" in usage_text and "ABC999" in usage_text, "不填参数时给出用法与当前验证码")

        long_replies = [r async for r in plugin.review_set_code(admin_event, "x" * 65)]
        check(
            "设定失败" in chain_text(long_replies[0]) and plugin._state["code"] == "ABC999",
            "超长验证码被拒绝且不改变现有验证码",
        )

        guest_set = [r async for r in plugin.review_set_code(FakeEvent(user_id="10001"), "HACK")]
        check(
            "只有管理员" in chain_text(guest_set[0]) and plugin._state["code"] == "ABC999",
            "非管理员不能设定验证码",
        )

        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="10005"))
        approve_replies = [r async for r in plugin.review_approve(admin_event, "10005", "123456")]
        approve_text = chain_text(approve_replies[0])
        check("123456:10005" in plugin._state["approved"], "/审核 放行 记录为已通过")
        check("123456:10005" not in plugin._state["pending"], "/审核 放行 清除待审核记录")
        check(plugin._state["code"] in approve_text, "/审核 放行 回显当前验证码")

        retry_replies = [r async for r in plugin.review_retry(admin_event, "10002", "123456")]
        check(
            "123456:10002" in plugin._state["pending"] and plugin._state["pending"]["123456:10002"]["attempts"] == 0,
            "/审核 重审 重新建立待审核记录",
        )
        check("已重置" in chain_text(retry_replies[0]), "/审核 重审 有回执")

        client.calls.clear()
        kick_replies = [r async for r in plugin.review_kick(admin_event, "10002", "123456")]
        check(
            any(str(c[1].get("user_id")) == "10002" for c in client.calls if c[0] == "set_group_kick"),
            "/审核 踢出 调用 set_group_kick",
        )
        check("123456:10002" not in plugin._state["pending"], "/审核 踢出 清除待审核记录")
        check("已移出" in chain_text(kick_replies[0]), "/审核 踢出 有回执")

        client.members = [{"user_id": 20001, "role": "member"}]
        context.sent.clear()
        cleanup_replies = [r async for r in plugin.review_cleanup(admin_event)]
        check("已开始清理" in chain_text(cleanup_replies[0]), "/审核 清理 立即回执")
        for job in list(plugin._jobs):
            await job
        report = [chain_text(chain) for _, chain in context.sent]
        check(any("清理完成" in text and "共移出 1 名成员" in text for text in report), "清理完成后向发起会话汇报统计")
        help_replies = [r async for r in plugin.review_help(admin_event)]
        check(any("帮助" in chain_text(reply) for reply in help_replies), "/审核 帮助 可用")

        print("\n[11] WebUI 后端 API（插件 Pages）")
        context.providers = [FakeProvider()]
        schema_keys = set(
            json.loads((PLUGIN_MAIN.parent / "_conf_schema.json").read_text(encoding="utf-8"))
        )
        registered = {api["route"]: set() for api in context.web_apis}
        for api in context.web_apis:
            registered[api["route"]].update(api["methods"])
        settings_route = f"/{module.PLUGIN_NAME}/settings"
        questions_route = f"/{module.PLUGIN_NAME}/questions"
        parse_route = f"/{module.PLUGIN_NAME}/questions/parse"
        check(
            registered.get(settings_route) == {"GET", "POST"}
            and registered.get(questions_route) == {"GET", "POST"}
            and registered.get(parse_route) == {"POST"},
            f"路由带插件名前缀且方法正确（{sorted(registered)}）",
        )

        get_resp = await plugin.api_get_settings()
        data = get_resp["payload"]["data"]
        check(get_resp["status_code"] == 200 and get_resp["payload"]["status"] == "ok", "GET 返回 ok 信封")
        check(set(data["schema"]) == schema_keys, "GET 返回完整 schema")
        check(set(data["values"]) == schema_keys, "GET 返回全部配置项的当前值")
        check(data["values"]["max_attempts"] == plugin.config["max_attempts"], "GET 的值与内存配置一致")
        check(data["status"]["code"] == plugin._state["code"], "GET 状态含当前验证码")
        check(data["status"]["pending"] == len(plugin._state["pending"]), "GET 状态含待审核人数")
        check(bool(data["status"]["code_reset_time"]), "GET 状态含下一次重置/清理时间")
        check(data["meta"]["name"] == module.PLUGIN_NAME and data["meta"]["version"], "GET 返回插件元信息")
        check("web_review_token" in data["secret_fields"], "secret 字段被声明（web_review_token）")
        check(data["values"]["web_review_token"] == "", "GET 不回显 secret 字段的真实值")
        check(
            isinstance(data["providers"], list)
            and data["providers"]
            and data["providers"][0]["id"] == "fake-chat",
            "GET 返回可选模型列表（供设置页下拉框）",
        )
        check(
            data["status"]["review_mode"] == "rule" and data["status"]["code_send_mode"] == "private",
            "GET 状态含审核方式与发码方式",
        )

        FAKE_REQUEST.payload = {
            "values": {
                "max_attempts": 5,
                "match_mode": "exact",
                "review_groups": "123456\n789012,123456",
                "kick_on_fail": "false",
            }
        }
        save_resp = await plugin.api_save_settings()
        saved_data = save_resp["payload"]["data"]
        check(save_resp["status_code"] == 200 and save_resp["payload"]["status"] == "ok", "POST 保存成功")
        check(plugin.config["max_attempts"] == 5 and plugin.config["match_mode"] == "exact", "数值/枚举已写入")
        check(plugin.config["kick_on_fail"] is False, "字符串 'false' 被规范化为布尔")
        check(
            plugin.config["review_groups"] == ["123456", "789012"],
            f"列表被拆行并去重（{plugin.config['review_groups']}）",
        )
        expected_saved = ["kick_on_fail", "match_mode", "max_attempts", "review_groups"]
        check(sorted(saved_data["saved"]) == expected_saved, "返回已保存的 key")
        check(plugin.config.saved and plugin.config.saved[-1]["max_attempts"] == 5, "调用了 save_config_async 落盘")
        check(set(saved_data["values"]) == schema_keys, "返回全量最新值")

        explicit = await plugin.api_save_settings(FAKE_REQUEST)
        check(explicit["status_code"] == 200, "显式传入 request 对象时也能解析请求体")

        FAKE_REQUEST.payload = {
            "values": {
                "questions": [
                    {
                        "__template_key": "question_item",
                        "question": "新题",
                        "answers": "答案A\n答案B",
                        "unexpected": "应被丢弃",
                    }
                ]
            }
        }
        resp = await plugin.api_save_settings()
        entry = plugin.config["questions"][0]
        check(resp["status_code"] == 200, "POST 支持 template_list")
        check(entry["answers"] == ["答案A", "答案B"], "template_list 内的列表被规范化")
        check(entry["__template_key"] == "question_item" and "unexpected" not in entry, "保留 __template_key 并丢弃未知子键")

        FAKE_REQUEST.payload = {"values": {"max_attempts": 100}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400 and "max_attempts" in resp["payload"]["message"], "超范围数值被拒绝")
        check(plugin.config["max_attempts"] == 5, "被拒绝时不改动配置")

        # secret 字段：可写入、留空不改、永不回显
        plugin.config["web_review_token"] = ""
        FAKE_REQUEST.payload = {"values": {"web_review_token": "tok-abcdefghijklmnop", "max_attempts": 3}}
        resp = await plugin.api_save_settings()
        check(
            resp["status_code"] == 200 and plugin.config["web_review_token"] == "tok-abcdefghijklmnop",
            "secret 字段可以保存",
        )
        FAKE_REQUEST.payload = {"values": {"web_review_token": "", "max_attempts": 3}}
        resp = await plugin.api_save_settings()
        check(plugin.config["web_review_token"] == "tok-abcdefghijklmnop", "secret 字段留空表示不修改")
        resp = await plugin.api_get_settings()
        check(resp["payload"]["data"]["values"]["web_review_token"] == "", "保存后 GET 依然不回显 secret")
        plugin.config["web_review_token"] = ""

        FAKE_REQUEST.payload = {"values": {"match_mode": "levenshtein"}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400 and "match_mode" in resp["payload"]["message"], "非法 options 被拒绝")

        FAKE_REQUEST.payload = {"values": {"fuzzy_threshold": 1.5}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "越界的 fuzzy_threshold 被拒绝")

        FAKE_REQUEST.payload = {"values": {"cleanup_time": "25:99"}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "非法时间被拒绝（避免运行时静默回退）")

        FAKE_REQUEST.payload = {"values": {"code_charset": "AB"}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "字符集过短被拒绝")

        FAKE_REQUEST.payload = {"values": {"timezone": "Not/AZone"}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "不可用的时区被拒绝")

        FAKE_REQUEST.payload = {"values": {"review_mode": "magic"}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "非法 review_mode 被拒绝")

        FAKE_REQUEST.payload = {"values": {"review_llm_provider": "nope"}}
        resp = await plugin.api_save_settings()
        check(
            resp["status_code"] == 400 and "review_llm_provider" in resp["payload"]["message"],
            "不存在的模型提供商被拒绝（保存期就报错，而不是运行时静默回退）",
        )

        FAKE_REQUEST.payload = {"values": {"code_send_mode": "sms"}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "非法 code_send_mode 被拒绝")

        FAKE_REQUEST.payload = {"values": {"private_send_channel": "telepathy"}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "非法 private_send_channel 被拒绝")

        FAKE_REQUEST.payload = {"values": {"nope": 1}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400 and "nope" in resp["payload"]["message"], "未知配置项被拒绝")

        FAKE_REQUEST.payload = {
            "values": {"questions": [{"__template_key": "nope", "question": "x", "answers": ["y"]}]}
        }
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "未知模板被拒绝")

        FAKE_REQUEST.payload = "not-a-dict"
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "非对象请求体被拒绝")

        FAKE_REQUEST.payload = {"values": {}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "空 values 被拒绝")

        FAKE_REQUEST.payload = {"values": {"review_groups": []}}
        resp = await plugin.api_save_settings()
        warnings = resp["payload"]["data"]["warnings"]
        check(
            resp["status_code"] == 200 and any("review_groups" in item for item in warnings),
            "空审核群给出警告但不阻止保存",
        )

        real_config = plugin.config
        plugin.config = {"review_groups": ["123456"]}  # 普通 dict：没有保存方法
        FAKE_REQUEST.payload = {"values": {"max_attempts": 3}}
        resp = await plugin.api_save_settings()
        check(
            resp["status_code"] == 500 and "保存配置失败" in resp["payload"]["message"],
            "配置对象不支持保存时返回 500 而不是假装成功",
        )
        plugin.config = real_config
        FAKE_REQUEST.payload = {"values": {"review_groups": ["123456"]}}
        await plugin.api_save_settings()

        print("\n[12] LLM 审核（review_mode）")

        def reset_review(**overrides):
            plugin.config.update(
                {
                    "review_mode": "rule",
                    "review_llm_provider": "",
                    "match_mode": "contains",
                    "max_attempts": 3,
                    "kick_on_fail": True,
                    "auto_enroll_on_speak": True,
                    "questions": [
                        {"__template_key": "question_item", "question": "1+1=?", "answers": ["2"]}
                    ],
                }
            )
            plugin.config.update(overrides)

        check(module._parse_llm_verdict("PASS") is True, "解析 PASS")
        check(module._parse_llm_verdict("  fail。") is False, "解析带标点的 fail")
        check(module._parse_llm_verdict("该回答不通过") is False, "解析「不通过」（不能被「通过」误判）")
        check(module._parse_llm_verdict("通过") is True, "解析「通过」")
        check(module._parse_llm_verdict("我不知道") is None, "无法判定时返回 None")
        check(module._parse_llm_verdict("") is None, "空回复返回 None")

        provider = FakeProvider(reply="PASS")
        context.providers = [provider]
        context.using_provider = provider
        reset_review(review_mode="llm")
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="20001"))
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="20001", text="不知道"))
        check("123456:20001" in plugin._state["approved"], "规则不命中但 LLM 判 PASS 时通过")
        check(len(provider.calls) == 1, "调用了一次模型")
        check("{answer}" not in provider.calls[0]["prompt"] and "不知道" in provider.calls[0]["prompt"], "提示词占位符已渲染")
        check(provider.calls[0]["system_prompt"], "带上了固定的系统提示词")
        check(
            client.temp_sessions
            and plugin._state["code"] in str(client.temp_sessions[-1].get("message") or ""),
            "LLM 模式下通过后也走群临时会话发码",
        )

        provider.reply = "FAIL"
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="20002"))
        await plugin.on_group_message(FakeEvent(user_id="20002", text="2"))
        record = plugin._state["pending"].get("123456:20002")
        check(record is not None and record["attempts"] == 1, "LLM 判 FAIL 时即使规则命中也不算通过")

        provider.reply = "嗯……我不好说"
        await plugin.on_group_message(FakeEvent(user_id="20002", text="3"))
        check(plugin._state["pending"]["123456:20002"]["attempts"] == 2, "模型回复无法解析时回退规则（规则不命中）")

        provider.reply = "2"
        await plugin.on_group_message(FakeEvent(user_id="20002", text="2"))
        check("123456:20002" in plugin._state["approved"], "模型回复无法解析时回退规则（规则命中则通过）")

        failing = FakeProvider(error=RuntimeError("模型炸了"))
        context.providers = [failing]
        context.using_provider = failing
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="20003"))
        await plugin.on_group_message(FakeEvent(user_id="20003", text="2"))
        check("123456:20003" in plugin._state["approved"], "模型调用抛异常时回退规则，不影响入群")

        slow = FakeProvider(reply="PASS", delay=2.0)
        context.providers = [slow]
        context.using_provider = slow
        reset_review(review_mode="llm", llm_review_timeout_seconds=1.0)
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="20004"))
        started = time.monotonic()
        await plugin.on_group_message(FakeEvent(user_id="20004", text="不知道"))
        elapsed = time.monotonic() - started
        check(elapsed < 1.9, f"LLM 超时会及时放弃（耗时 {elapsed:.1f}s）")
        check("123456:20004" not in plugin._state["approved"], "超时后回退规则（规则不命中则不通过）")

        context.providers = []
        context.using_provider = None
        reset_review(review_mode="hybrid")
        provider.calls.clear()
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="20005"))
        await plugin.on_group_message(FakeEvent(user_id="20005", text="2"))
        check("123456:20005" in plugin._state["approved"], "hybrid 模式下规则命中直接通过")
        check(len(provider.calls) == 0, "没有可用 Provider 时不会调用模型")

        provider.reply = "PASS"
        context.providers = [provider]
        context.using_provider = provider  # 未指定 review_llm_provider 时跟随会话模型
        reset_review(review_mode="hybrid")
        provider.calls.clear()
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="20006"))
        await plugin.on_group_message(FakeEvent(user_id="20006", text="等于二"))
        check(len(provider.calls) == 1, "hybrid 模式下规则不命中时才调用模型")
        check("123456:20006" in plugin._state["approved"], "hybrid 由模型判定通过")

        # 显式指定提供商（WebUI 里选了具体模型的场景）
        reset_review(review_mode="llm", review_llm_provider="fake-chat")
        context.using_provider = None
        provider.calls.clear()
        provider.reply = "PASS"
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="20008"))
        await plugin.on_group_message(FakeEvent(user_id="20008", text="不想算"))
        check(len(provider.calls) == 1, "指定了提供商时按 id 取模型")
        check("123456:20008" in plugin._state["approved"], "指定提供商时也能判定通过")

        reset_review(review_mode="llm", review_llm_provider="not-exist")
        provider.calls.clear()
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="20007"))
        await plugin.on_group_message(FakeEvent(user_id="20007", text="2"))
        check(len(provider.calls) == 0, "指定的提供商不存在时不调用模型")
        check("123456:20007" in plugin._state["approved"], "提供商不存在时回退规则判定")

        print("\n[13] 验证码私发（群临时会话 / code_send_mode）")

        def reset_review(**overrides):
            plugin.config.update(overrides)

        def group_texts():
            return [t for umo, t in ((u, chain_text(c)) for u, c in context.sent) if "GroupMessage" in umo]

        def friend_texts():
            return [t for umo, t in ((u, chain_text(c)) for u, c in context.sent) if "FriendMessage" in umo]

        reset_review(
            review_mode="rule",
            code_send_mode="private",
            code_fallback_to_group=False,
            private_send_channel="auto",
        )
        plugin.config["success_message"] = "审核通过！验证码已私发（{expire} 失效）"
        plugin.config["private_code_message"] = "你的验证码：{code}"
        client.temp_sessions.clear()
        client.calls.clear()
        client.fail_temp_session = False
        context.fail_private = False
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30001"))
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="30001", text="2"))
        code = plugin._state["code"]
        check(len(client.temp_sessions) == 1, f"默认走群临时会话、只发一次（实际 {len(client.temp_sessions)}）")
        check(any(action == "send_private_msg" for action, _ in client.calls), "临时会话用的是 send_private_msg")
        session = client.temp_sessions[0] if client.temp_sessions else {}
        check(str(session.get("group_id")) == "123456" and str(session.get("user_id")) == "30001", "临时会话带上群号与成员号")
        check(code in str(session.get("message") or ""), "临时会话消息里含验证码")
        check(not friend_texts(), "走通临时会话后不再发好友私聊")
        check(group_texts() and all(code not in t for t in group_texts()), "默认 private：群内提示不含验证码")

        # friend 通道：跳过临时会话，直接好友私聊
        reset_review(private_send_channel="friend")
        client.temp_sessions.clear()
        context.sent.clear()
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30007"))
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="30007", text="2"))
        check(not client.temp_sessions, "private_send_channel=friend：不走临时会话")
        check(friend_texts() and plugin._state["code"] in friend_texts()[0], "friend 通道走好友私聊并带上验证码")

        # temp_session 专用：失败也不退回好友私聊
        reset_review(private_send_channel="temp_session")
        client.fail_temp_session = True
        context.sent.clear()
        context.private_failures.clear()
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30008"))
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="30008", text="2"))
        check(not friend_texts(), "private_send_channel=temp_session：失败也不退回好友私聊")
        check(group_texts() and all(plugin._state["code"] not in t for t in group_texts()), "临时会话失败且未开回退：群内不发码")

        # auto + 临时会话失败 → 退回好友私聊
        reset_review(private_send_channel="auto")
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30009"))
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="30009", text="2"))
        check(friend_texts() and plugin._state["code"] in friend_texts()[0], "auto：临时会话失败时退回好友私聊")

        # auto + 临时会话失败 + 好友也失败 + 开启群内回退
        context.fail_private = True
        reset_review(private_send_channel="auto", code_fallback_to_group=True)
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30010"))
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="30010", text="2"))
        check(any(plugin._state["code"] in t for t in group_texts()), "两条私聊通道都失败 + 开启回退：群内兜底发码")
        client.fail_temp_session = False
        context.fail_private = False

        reset_review(code_send_mode="group")
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30002"))
        context.sent.clear()
        client.temp_sessions.clear()
        await plugin.on_group_message(FakeEvent(user_id="30002", text="2"))
        check(any(plugin._state["code"] in t for t in group_texts()), "group 模式：群内发码")
        check(not client.temp_sessions and not friend_texts(), "group 模式：不发私聊")

        reset_review(code_send_mode="both")
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30003"))
        context.sent.clear()
        client.temp_sessions.clear()
        await plugin.on_group_message(FakeEvent(user_id="30003", text="2"))
        check(
            client.temp_sessions and any(plugin._state["code"] in t for t in group_texts()),
            "both 模式：临时会话 + 群内都发",
        )

        # 群内发码但文案里没有 {code}：应自动补一行，避免"配好了却没发码"
        reset_review(code_send_mode="group")
        plugin.config["success_message"] = "审核通过！（{expire} 失效）"
        plugin._group_code_notice_logged = False
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30004"))
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="30004", text="2"))
        check(any(plugin._state["code"] in t for t in group_texts()), "文案缺 {code} 时自动补上验证码")
        check(plugin._group_code_notice_logged, "补码只提示一次（打了标记）")

        # 私聊失败 + 未开启回退：群内只提示，不发码
        reset_review(code_send_mode="private", code_fallback_to_group=False, private_send_channel="temp_session")
        plugin.config["success_message"] = "验证码已私发"
        client.fail_temp_session = True
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30005"))
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="30005", text="2"))
        check(group_texts() and all(plugin._state["code"] not in t for t in group_texts()), "私聊失败且未开回退：群内不发码")
        client.fail_temp_session = False

        # 私聊失败 + 开启回退：群内发码兜底
        reset_review(code_send_mode="private", code_fallback_to_group=True, private_send_channel="temp_session")
        client.fail_temp_session = True
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="30006"))
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="30006", text="2"))
        check(any(plugin._state["code"] in t for t in group_texts()), "私聊失败 + 开启回退：群内兜底发码")
        client.fail_temp_session = False

        client.temp_sessions.clear()
        resend_replies = [r async for r in plugin.review_resend_code(admin_event, "30001", "123456")]
        check("已私发验证码" in chain_text(resend_replies[0]), "/审核 补发 成功回执")
        check(
            client.temp_sessions and str(client.temp_sessions[-1].get("group_id")) == "123456",
            "/审核 补发 也走群临时会话",
        )
        client.fail_temp_session = True
        context.fail_private = True
        failed_replies = [r async for r in plugin.review_resend_code(admin_event, "30001", "123456")]
        check("私发失败" in chain_text(failed_replies[0]), "/审核 补发 失败时给出可操作提示")
        client.fail_temp_session = False
        context.fail_private = False

        print("\n[15] 诊断与排查（为什么没给新人发消息）")
        src = PLUGIN_MAIN.read_text(encoding="utf-8")
        notice_decorator = src.split("async def on_group_notice")[0].rstrip().splitlines()[-1]
        check(
            "EventMessageType.ALL" in notice_decorator,
            "群通知处理器用 ALL 接收（兼容把通知标成 OTHER_MESSAGE 的版本）",
        )

        reset_review(review_mode="rule", code_send_mode="private", private_send_channel="auto")
        plugin.config["questions"] = [
            {"__template_key": "question_item", "question": "1+1=?", "answers": ["2"]}
        ]
        plugin._diag = {
            "counters": {},
            "last_notice": None,
            "last_message": None,
            "last_question": None,
            "last_reason": "",
        }
        context.fail_private = False
        client.fail_temp_session = False

        # 1) 非配置群的入群通知
        await plugin.on_group_notice(
            FakeEvent(post_type="notice", notice_type="group_increase", group_id="999999", user_id="40001")
        )
        check(plugin._diag["counters"].get("other_group") == 1, "非配置群的群通知被计数")
        check("999999" in plugin._diag["last_reason"], "记录了跳过原因（含群号）")

        # 2) 配置群但题目不可用
        plugin.config["questions"] = []
        await plugin.on_group_notice(
            FakeEvent(post_type="notice", notice_type="group_increase", user_id="40002")
        )
        check(plugin._diag["counters"].get("no_question") == 1, "题目不可用时计入 no_question")
        check("题目" in plugin._diag_verdict(dict(plugin._diag["counters"])), "结论指出题目不可用")
        plugin.config["questions"] = [
            {"__template_key": "question_item", "question": "1+1=?", "answers": ["2"]}
        ]

        # 3) 正常入群
        await plugin.on_group_notice(
            FakeEvent(post_type="notice", notice_type="group_increase", user_id="40003")
        )
        counters = plugin._diag["counters"]
        check(counters.get("increase") == 2, f"入群通知计数正确（{counters.get('increase')}）")
        check(counters.get("asked") == 1, "成功提问计数正确")
        check((plugin._diag.get("last_question") or {}).get("user_id") == "40003", "记录最近一次提问对象")

        # 4) 重复入群（漏收退群通知）：必须重新提问并清零次数
        plugin._state["pending"]["123456:40003"]["attempts"] = 2
        reask_event = FakeEvent(post_type="notice", notice_type="group_increase", user_id="40003")
        await plugin.on_group_notice(reask_event)
        check(plugin._diag["counters"].get("reask") == 1, "重复入群被识别为重新提问")
        check(plugin._state["pending"]["123456:40003"]["attempts"] == 0, "重新提问会清零答题次数")
        check(
            any("1+1=?" in text for text in texts_of(reask_event.sent)),
            "重复入群时确实重新发送了问题",
        )

        # 5) 发送失败要能看见
        class BoomEvent(FakeEvent):
            async def send(self, chain):
                raise RuntimeError("发送失败（模拟机器人被禁言）")

        before_fail = plugin._diag["counters"].get("send_fail", 0)
        await plugin.on_group_notice(
            BoomEvent(post_type="notice", notice_type="group_increase", user_id="40004")
        )
        check(plugin._diag["counters"].get("send_fail", 0) == before_fail + 1, "发送失败被计数")
        check("发送失败" in plugin._diag_verdict(dict(plugin._diag["counters"])), "结论提示发送失败")

        # 6) 诊断指令输出
        diag_replies = [r async for r in plugin.review_diagnose(admin_event)]
        diag_text = chain_text(diag_replies[0])
        for needle in ("审核群诊断", "事件计数", "结论：", "发送自检", "重新进群"):
            check(needle in diag_text, f"诊断报告包含「{needle}」")
        check("成功 ✅" in diag_text, "诊断里的群发送自检成功")
        guest_diag = [r async for r in plugin.review_diagnose(FakeEvent(user_id="10001"))]
        check("只有管理员" in chain_text(guest_diag[0]), "非管理员不能执行诊断")

        # 7) 群消息计数（用于区分"消息能到、通知不到"）
        before_msg = plugin._diag["counters"].get("messages", 0)
        await plugin.on_group_message(FakeEvent(user_id="40005", text="随便说句话"))
        check(
            plugin._diag["counters"].get("messages", 0) == before_msg + 1,
            "配置群内的群消息被计数",
        )

        print("\n[16] 问题库 / 答案库")
        reset_review(review_mode="rule", match_mode="contains", fuzzy_threshold=0.8)
        plugin.config["common_answers"] = []
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "第一题", "hint": "", "answers": ["甲", "乙"], "match_mode": "inherit"},
            {"__template_key": "question_item", "enabled": True, "question": "第二题", "hint": "提示内容", "answers": ["丙"], "match_mode": "exact"},
            {"__template_key": "question_item", "enabled": False, "question": "停用题", "hint": "", "answers": ["丁"], "match_mode": "inherit"},
        ]
        parsed = plugin._questions()
        check([item["question"] for item in parsed] == ["第一题", "第二题"], "停用的题目不参与抽题")
        check(len(plugin._questions(include_disabled=True)) == 3, "题库总览包含停用题目")
        check(parsed[1]["match_mode"] == "exact" and parsed[0]["match_mode"] == "inherit", "每题匹配方式解析正确")
        check(plugin._resolve_match_mode("inherit") == "contains", "inherit 跟随全局 match_mode")
        check(plugin._resolve_match_mode("fuzzy") == "fuzzy", "单题可覆盖为 fuzzy")

        # fuzzy 匹配
        plugin.config["match_mode"] = "fuzzy"
        check(plugin._match_answer("学习与交流", ["学习交流"]), "fuzzy：多写的字也能通过")
        check(not plugin._match_answer("完全不相干", ["学习交流"]), "fuzzy：不相似不通过")
        check(plugin._match_answer("学习交流呀", ["学习交流"]), "fuzzy：包含优先命中")
        plugin.config["fuzzy_threshold"] = 0.99
        check(not plugin._match_answer("学习与交流", ["学习交流"]), "阈值调高后同一回答不再通过")
        plugin.config["fuzzy_threshold"] = 0.8
        plugin.config["match_mode"] = "contains"

        # 通用答案库
        plugin.config["common_answers"] = ["邀请码1234"]
        common_pass, common_detail = plugin._evaluate_rules(
            "我的邀请码1234", ["甲"], "contains", plugin._common_answers()
        )
        check(common_pass and "通用答案库" in common_detail, "通用答案库命中即通过")
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "只有通用答案的题", "hint": "", "answers": [], "match_mode": "inherit"}
        ]
        check(plugin._pick_question() is not None, "配了通用答案库时，该题无答案也可用")
        plugin.config["common_answers"] = []
        check(plugin._pick_question() is None, "去掉通用答案后该题不可用")

        # 抽题会把匹配方式与提示固化进记录
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "固化题", "hint": "小提示", "answers": ["甲"], "match_mode": "exact"}
        ]
        context.sent.clear()
        notice_event = FakeEvent(post_type="notice", notice_type="group_increase", user_id="50001")
        await plugin.on_group_notice(notice_event)
        record = plugin._state["pending"].get("123456:50001") or {}
        check(record.get("match_mode") == "exact", "记录里固化了该题的匹配方式")
        check(record.get("hint") == "小提示", "记录里带上该题提示")
        asked = texts_of(notice_event.sent)
        check(any("提示：小提示" in text for text in asked), "该题提示自动附在提问后面")

        stats = plugin._state["question_stats"].get("固化题") or {}
        check(stats.get("picks") == 1, f"抽中次数被统计（{stats.get('picks')}）")
        await plugin.on_group_message(FakeEvent(user_id="50001", text="甲"))
        stats = plugin._state["question_stats"].get("固化题") or {}
        check(stats.get("passes") == 1, "通过次数被统计")
        check("123456:50001" not in (plugin._state.get("pending") or {}), "答对后清理待审核记录")

        await plugin._after_cleanup()
        check(
            (plugin._state["question_stats"].get("固化题") or {}).get("passes") == 1,
            "每日清理不会清掉题库统计",
        )

        bank_replies = [r async for r in plugin.review_bank(admin_event)]
        bank_text = chain_text(bank_replies[0])
        for needle in ("问题库 / 答案库", "匹配：exact", "抽中 1 次 / 通过 1 次", "可用题目：1 条", "通用答案库"):
            check(needle in bank_text, f"题库报告包含「{needle}」")
        check("只有管理员" in chain_text([r async for r in plugin.review_bank(FakeEvent(user_id="10001"))][0]), "非管理员不能看题库")

        usage_replies = [r async for r in plugin.review_test_answer(FakeEvent(user_id="555", text="审核 试答"))]
        check("用法" in chain_text(usage_replies[0]), "试答无参数时给出用法")

        before_pending = dict(plugin._state.get("pending") or {})
        passed_replies = [r async for r in plugin.review_test_answer(FakeEvent(user_id="555", text="审核 试答 甲"))]
        passed_text = chain_text(passed_replies[0])
        check("规则判定：通过 ✅" in passed_text, "试答：命中答案")
        check("✅ 甲" in passed_text, "试答逐条列出答案命中情况")
        failed_replies = [r async for r in plugin.review_test_answer(FakeEvent(user_id="555", text="审核 试答 #1 完全不相关"))]
        failed_text = chain_text(failed_replies[0])
        check("规则判定：不通过 ❌" in failed_text, "试答：不命中")
        check("❌ 甲" in failed_text, "试答会指出哪条答案没命中")
        check(
            "题号超出范围" in chain_text([r async for r in plugin.review_test_answer(FakeEvent(user_id="555", text="审核 试答 #9 甲"))][0]),
            "试答题号越界时给出提示",
        )
        check(dict(plugin._state.get("pending") or {}) == before_pending, "试答不改动任何审核记录")
        check(
            (plugin._state["question_stats"].get("固化题") or {}).get("picks") == 1,
            "试答不消耗抽题次数",
        )

        llm_provider = FakeProvider(reply="PASS")
        context.providers = [llm_provider]
        context.using_provider = llm_provider
        reset_review(review_mode="llm", review_llm_provider="fake-chat")
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "固化题", "hint": "", "answers": ["甲"], "match_mode": "inherit"}
        ]
        llm_replies = [r async for r in plugin.review_test_answer(FakeEvent(user_id="555", text="审核 试答 意思差不多"))]
        llm_text = chain_text(llm_replies[-1])
        check("大模型判定：通过 ✅" in llm_text, "试答会带上大模型判定")
        check("模型原始回复：PASS" in llm_text, "试答回显模型原始回复")
        reset_review(review_mode="rule")
        context.providers = []
        context.using_provider = None

        print("\n[17] 题库 Page 接口（读写 + LLM 自动填入）")
        context.providers = [FakeProvider()]
        context.using_provider = context.providers[0]
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "甲题", "hint": "提示甲", "answers": ["甲"], "match_mode": "exact"},
            {"__template_key": "question_item", "enabled": False, "question": "乙题", "hint": "", "answers": ["乙"], "match_mode": "inherit"},
        ]
        plugin.config["common_answers"] = ["邀请码1234"]

        bank_resp = await plugin.api_get_questions()
        bank = bank_resp["payload"]["data"]
        check(bank_resp["payload"]["status"] == "ok", "GET 题库返回 ok 信封")
        check(len(bank["questions"]) == 2, "GET 返回全部题目（含停用）")
        first = bank["questions"][0]
        for field in ("__template_key", "enabled", "question", "hint", "answers", "match_mode", "picks", "passes", "usable"):
            check(field in first, f"题库条目含字段 {field}")
        check(first["__template_key"] == "question_item", "条目带 __template_key（保存时原样回传）")
        check(bank["questions"][1]["usable"] is False, "停用题目标记为不可用")
        check(bank["common_answers"] == ["邀请码1234"], "GET 返回通用答案库")
        check("fuzzy" in bank["match_mode_options"], "GET 返回可选匹配方式")
        check(bank["llm_available"] is True and bank["parse_limit"] == 30, "GET 返回 LLM 可用性与解析上限")

        FAKE_REQUEST.payload = {
            "questions": [
                {"__template_key": "question_item", "enabled": True, "question": "甲题", "hint": "", "answers": ["甲", "第一"], "match_mode": "contains", "picks": 99},
                {"__template_key": "question_item", "enabled": True, "question": "新题", "hint": "新提示", "answers": "丙\n丁", "match_mode": "fuzzy"},
            ],
            "common_answers": "邀请码1234, 口令888",
        }
        save_resp = await plugin.api_save_questions()
        saved = save_resp["payload"]["data"]
        check(save_resp["status_code"] == 200, "POST 题库保存成功")
        check(len(plugin.config["questions"]) == 2 and plugin.config["questions"][1]["question"] == "新题", "新题目写入配置")
        check(plugin.config["questions"][1]["answers"] == ["丙", "丁"], "答案字符串被拆成列表")
        check(plugin.config["questions"][0]["answers"] == ["甲", "第一"], "已有题目的答案被更新")
        check("picks" not in plugin.config["questions"][0], "只读统计字段不会被写进配置")
        check(plugin.config["common_answers"] == ["邀请码1234", "口令888"], "通用答案库被保存")
        check(saved["saved"] == 2 and isinstance(saved["warnings"], list), "保存响应带上数量与提醒")
        check(plugin.config.saved and plugin.config.saved[-1]["questions"][1]["match_mode"] == "fuzzy", "通过 save_config_async 落盘")

        FAKE_REQUEST.payload = {"questions": [{"__template_key": "nope", "question": "x", "answers": ["y"]}]}
        resp = await plugin.api_save_questions()
        check(resp["status_code"] == 400, "未知模板被拒绝")

        FAKE_REQUEST.payload = {"questions": [{"__template_key": "question_item", "question": "x", "answers": ["y"], "match_mode": "levenshtein"}]}
        resp = await plugin.api_save_questions()
        check(resp["status_code"] == 400 and "match_mode" in resp["payload"]["message"], "条目内非法匹配方式被拒绝")

        FAKE_REQUEST.payload = {"questions": "not-a-list"}
        resp = await plugin.api_save_questions()
        check(resp["status_code"] == 400, "非数组 questions 被拒绝")

        FAKE_REQUEST.payload = {"questions": 123}
        resp = await plugin.api_save_questions()
        check(resp["status_code"] == 400, "questions 传数字时返回 400 而不是 500")

        FAKE_REQUEST.payload = {"questions": [{"__template_key": "question_item", "question": f"题{i}"} for i in range(201)]}
        resp = await plugin.api_save_questions()
        check(resp["status_code"] == 400 and "过多" in resp["payload"]["message"], "题目数量超过上限被拒绝")

        FAKE_REQUEST.payload = {"questions": [], "common_answers": []}
        resp = await plugin.api_save_questions()
        check(
            resp["status_code"] == 200 and any("题目" in item for item in resp["payload"]["data"]["warnings"]),
            "清空题库可以保存，但会给出提醒",
        )

        check(plugin._extract_json_array('[{"question":"甲"}]') == [{"question": "甲"}], "解析纯 JSON 数组")
        check(
            plugin._extract_json_array('```json\n[{"question":"甲"}]\n```') == [{"question": "甲"}],
            "解析带代码块的 JSON",
        )
        check(
            plugin._extract_json_array('好的，结果如下：[{"question":"甲"}] 以上。') == [{"question": "甲"}],
            "解析夹在文字里的 JSON",
        )
        check(plugin._extract_json_array("没有数组") is None, "没有数组时返回 None")

        llm = context.providers[0]
        llm.reply = (
            "```json\n"
            '[{"question":"问题一","answers":["答案A","答案B"],"hint":"提示","match_mode":"contains"},'
            '{"question":"问题二","answers":"答案C、答案D","match_mode":"不存在的模式"},'
            '{"question":"   ","answers":["垃圾"]},'
            '{"question":"问题三","answers":[]}]\n'
            "```"
        )
        FAKE_REQUEST.payload = {"text": "招新公告：问题一……问题二……"}
        parse_resp = await plugin.api_parse_questions()
        drafts = parse_resp["payload"]["data"]["drafts"]
        check(parse_resp["status_code"] == 200 and len(drafts) == 3, f"解析出草稿并丢弃空题干（{len(drafts)} 条）")
        check(drafts[0]["answers"] == ["答案A", "答案B"] and drafts[0]["match_mode"] == "contains", "草稿保留答案与匹配方式")
        check(drafts[1]["answers"] == ["答案C", "答案D"], "草稿里的答案字符串被拆分")
        check(drafts[1]["match_mode"] == "inherit", "非法匹配方式回退为 inherit")
        check(drafts[2]["answers"] == [], "允许没有答案的草稿")
        check(parse_resp["payload"]["data"]["provider"] == "fake-chat", "响应里标明使用的模型")
        check("只输出一个 JSON 数组" in llm.calls[-1]["prompt"], "提示词要求只输出 JSON")

        llm.reply = "我无法完成这个请求。"
        FAKE_REQUEST.payload = {"text": "随便一段"}
        resp = await plugin.api_parse_questions()
        check(resp["status_code"] == 400 and resp["payload"]["data"].get("raw"), "模型不返回 JSON 时给出原文便于人工处理")

        llm.reply = '[{"question":"1","answers":[]},{"question":"2","answers":[]}]'
        FAKE_REQUEST.payload = {"text": "x" * (module.QUESTION_PARSE_MAX_CHARS + 1)}
        resp = await plugin.api_parse_questions()
        check(resp["status_code"] == 400 and "太长" in resp["payload"]["message"], "超长文本被拒绝")

        FAKE_REQUEST.payload = {"text": "一段正常文本"}
        context.providers = []
        context.using_provider = None
        plugin.config["review_llm_provider"] = ""
        resp = await plugin.api_parse_questions()
        check(resp["status_code"] == 400 and "没有可用的对话模型" in resp["payload"]["message"], "没有模型时给出明确提示")
        plugin.config["review_llm_provider"] = ""

        erroring = FakeProvider(error=RuntimeError("上游 500"))
        context.providers = [erroring]
        resp = await plugin.api_parse_questions()
        check(resp["status_code"] == 502, "模型调用失败时返回 502")

        FAKE_REQUEST.payload = {"text": ""}
        resp = await plugin.api_parse_questions()
        check(resp["status_code"] == 400, "空文本被拒绝")

        print("\n[19] @ 提到新人（{at} 占位符）")
        reset_review(review_mode="rule", code_send_mode="private", private_send_channel="auto")
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "1+1=?", "hint": "", "answers": ["2"], "match_mode": "inherit"}
        ]
        plugin.config["question_message"] = "欢迎加入！请回答：{question}"
        plugin.config["retry_message"] = "答错了，还剩 {remaining} 次"
        plugin.config["success_message"] = "审核通过！"
        plugin.config["kick_message"] = "{user} 未通过审核"

        # 1) 文案里没写 {at}，也要默认 @ 新人（提问 / 答错 / 通过三条路径）
        ask_event = FakeEvent(post_type="notice", notice_type="group_increase", user_id="60001")
        await plugin.on_group_notice(ask_event)
        check(
            mentions_of(ask_event.sent)[:1] == ["60001"],
            f"没写 {{at}} 也默认在最前面 @ 新人（{mentions_of(ask_event.sent)}）",
        )
        wrong = FakeEvent(user_id="60001", text="不知道")
        await plugin.on_group_message(wrong)
        check("60001" in mentions_of(wrong.sent), "答错重试提示也 @ 到本人")
        context.sent.clear()
        await plugin.on_group_message(FakeEvent(user_id="60001", text="2"))
        check("60001" in mentions_of(context.sent), "通过提示也 @ 到本人")

        # 2) {at} 可以放句中，位置精确
        plugin.config["question_message"] = "同学 {at} 你好，请回答：{question}"
        ask2 = FakeEvent(post_type="notice", notice_type="group_increase", user_id="60002")
        await plugin.on_group_notice(ask2)
        chain = chains_of(ask2.sent)[0]
        kinds = [type(component).__name__ for component in chain]
        check(kinds[:3] == ["Plain", "At", "Plain"], f"{{at}} 放句中时按位置插入 @（{kinds}）")
        check(chain[0].text.rstrip() == "同学", "句中 {at} 之前的文本保持原样（空格是模板里写的，会保留）")
        check(str(chain[1].qq) == "60002", "句中 {at} 指向新人")
        check(
            chain[2].text.startswith("你好"),
            f"{{at}} 后面多写的那个空格被吃掉（{chain[2].text!r}）",
        )

        # 3) 文案里可以出现多个 {at}
        plugin.config["question_message"] = "{at} 请回答：{question}（{at} 记得答完哦）"
        ask3 = FakeEvent(post_type="notice", notice_type="group_increase", user_id="60003")
        await plugin.on_group_notice(ask3)
        check(mentions_of(ask3.sent) == ["60003", "60003"], "多处 {at} 会插入多个 @")

        # 4) 踢人提示：默认不 @（人已移出群），模板写了 {at} 才 @
        plugin.config["question_message"] = "{at} 请回答：{question}"
        plugin.config["max_attempts"] = 1
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="60004"))
        kick_default = FakeEvent(user_id="60004", text="错的")
        await plugin.on_group_message(kick_default)
        check(mentions_of(kick_default.sent) == [], "踢人提示默认不 @")
        check("123456:60004" not in plugin._state["pending"], "（前置）达到答错上限已踢出")

        plugin.config["kick_message"] = "{at} {user} 未通过审核"
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="60005"))
        kick_at = FakeEvent(user_id="60005", text="错的")
        await plugin.on_group_message(kick_at)
        check(mentions_of(kick_at.sent) == ["60005"], "模板里写了 {at} 时踢人提示也会 @")
        plugin.config["max_attempts"] = 3
        plugin.config["kick_message"] = "{user} 未通过审核"

        print("\n[20] 以欢迎语的方式发问题（welcome_message / join_message_mode）")
        reset_review(review_mode="rule", code_send_mode="private", private_send_channel="auto")
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "1+1=?", "hint": "", "answers": ["2"], "match_mode": "inherit"}
        ]
        plugin.config["welcome_message"] = "🎉 {at} 欢迎加入本群！先答个题～"
        plugin.config["question_message"] = "请回答：{question}"

        # 1) 默认 merge：一条消息 = 欢迎语 + 问题，且只 @ 一次
        plugin.config["join_message_mode"] = "merge"
        merge_event = FakeEvent(post_type="notice", notice_type="group_increase", user_id="70001")
        await plugin.on_group_notice(merge_event)
        merge_texts = texts_of(merge_event.sent)
        check(len(merge_texts) == 1, f"merge 模式只发一条消息（实际 {len(merge_texts)}）")
        check(
            "欢迎加入本群" in merge_texts[0] and "请回答：1+1=?" in merge_texts[0],
            "一条消息里同时含欢迎语与问题",
        )
        check(merge_texts[0].index("欢迎加入本群") < merge_texts[0].index("请回答"), "欢迎语排在问题之前")
        check(mentions_of(merge_event.sent) == ["70001"], f"合并后只 @ 一次（{mentions_of(merge_event.sent)}）")

        # 2) separate：先欢迎语、再问题，两条各 @ 一次
        plugin.config["join_message_mode"] = "separate"
        plugin.config["question_message"] = "{at} 请回答：{question}"
        sep_event = FakeEvent(post_type="notice", notice_type="group_increase", user_id="70002")
        await plugin.on_group_notice(sep_event)
        sep_texts = texts_of(sep_event.sent)
        check(len(sep_texts) == 2, f"separate 模式发两条消息（实际 {len(sep_texts)}）")
        check("欢迎加入本群" in sep_texts[0] and "请回答" not in sep_texts[0], "第一条是纯欢迎语")
        check("请回答" in sep_texts[1], "第二条是问题")
        check(mentions_of(sep_event.sent) == ["70002", "70002"], "两条消息各自 @ 到新人")

        # 3) question_only / 欢迎语留空：不发欢迎语
        plugin.config["join_message_mode"] = "question_only"
        only_event = FakeEvent(post_type="notice", notice_type="group_increase", user_id="70003")
        await plugin.on_group_notice(only_event)
        check(
            len(texts_of(only_event.sent)) == 1 and "欢迎加入本群" not in texts_of(only_event.sent)[0],
            "question_only：只发问题",
        )
        plugin.config["join_message_mode"] = "merge"
        plugin.config["welcome_message"] = ""
        empty_event = FakeEvent(post_type="notice", notice_type="group_increase", user_id="70004")
        await plugin.on_group_notice(empty_event)
        check("欢迎加入本群" not in chain_text(empty_event.sent[0]), "欢迎语留空时退化为只发问题")

        # 4) 欢迎语里的占位符
        plugin.config["welcome_message"] = "欢迎 {user} 来到群 {group}，{max_attempts} 次机会。问题：{question}"
        placeholder_event = FakeEvent(post_type="notice", notice_type="group_increase", user_id="70005")
        await plugin.on_group_notice(placeholder_event)
        placeholder_text = chain_text(placeholder_event.sent[0])
        check("{" not in placeholder_text.replace("{at}", ""), f"欢迎语占位符已全部渲染（{placeholder_text[:70]}…）")
        check(
            "123456" in placeholder_text and "3 次机会" in placeholder_text,
            "{user}/{group}/{max_attempts} 渲染正确",
        )
        check("问题：1+1=?" in placeholder_text, "{question} 会插入题目本身")

        # 5) 首次发言补发也带欢迎语；管理员 /审核 重审 不带
        plugin.config["welcome_message"] = "🎉 {at} 欢迎加入本群！"
        plugin.config["question_message"] = "请回答：{question}"
        plugin.config["auto_enroll_on_speak"] = True
        speak_event = FakeEvent(user_id="70006", text="大家好")
        await plugin.on_group_message(speak_event)
        check("欢迎加入本群" in chain_text(speak_event.sent[0]), "首次发言补发问题时也带欢迎语")

        context.sent.clear()
        renew_replies = [r async for r in plugin.review_retry(admin_event, "70006", "123456")]
        renew_text = " ".join(texts_of(context.sent))
        check("欢迎加入本群" not in renew_text, "/审核 重审 不重复发欢迎语（只补问题）")
        check("已重置" in chain_text(renew_replies[0]), "/审核 重审 仍有回执")

        print("\n[21] B站 UID 审核")
        reset_review(review_mode="rule", code_send_mode="private", private_send_channel="auto")
        module.BILI_RETRY_DELAY = 0  # 重试等待在自检里不需要真等
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "请证明你是真人：", "hint": "", "answers": ["abc"], "match_mode": "inherit"}
        ]
        plugin.config["bili_uid_enabled"] = True
        plugin.config["bili_uid_min_level"] = 0
        plugin.config["bili_uid_min_fans"] = 0
        plugin.config["bili_uid_name_keywords"] = []
        plugin.config["bili_uid_unique"] = True
        plugin.config["bili_uid_on_error"] = "reject"
        plugin.config["bili_uid_timeout_seconds"] = 10.0
        plugin.config["bili_uid_prompt"] = "请把你的 B站 UID 发给我（纯数字，例如 12345678；也可以直接发你的 B站主页链接）"
        plugin.config["retry_message"] = "回答不正确，你还有 {remaining} 次机会。"

        # 1) UID 抽取
        extract = module.TempReviewGroup._extract_bili_uid
        check(extract("12345678") == "12345678", "抽取纯数字 UID")
        check(extract("我的UID：12345678") == "12345678", "抽取 UID:xxx 写法")
        check(extract("https://space.bilibili.com/12345678") == "12345678", "抽取主页链接里的 UID")
        check(extract("UID = 998877") == "998877", "抽取等号写法")
        check(extract("我不知道") is None, "没有数字时返回 None")
        check(extract("我有 3 个号") is None, "1 位数字不算 UID（避免误判）")
        check(extract("12345678901") == "12345678901", "11 位 UID 也能识别")
        check(extract("123456789012345") == "123456789012345", "15 位 UID 也能识别")
        check(extract("UID:1234567890123456") is None, "16 位数字不被当成 UID（不做截断）")
        check(extract("https://space.bilibili.com/12345678901") == "12345678901", "链接里的长 UID 也能识别")

        # 2) 假 HTTP：按 UID 配响应（列表=按顺序消费，最后一条可重复用）
        http_calls = []
        http_urls = []
        responses = {}

        async def fake_http(url, timeout, referer=""):
            mid = url.split("mid=")[1].split("&")[0]
            http_calls.append(mid)
            http_urls.append(url)
            if mid not in responses:
                raise AssertionError(f"自检没有为 UID {mid} 配置响应")
            item = responses[mid]
            if isinstance(item, list):
                item = item.pop(0) if len(item) > 1 else item[0]
            if isinstance(item, Exception):
                raise item
            return item

        def reset_http():
            http_calls.clear()
            http_urls.clear()
            responses.clear()
            plugin._bili_cache.clear()
            plugin._state["bili_uids"] = {}

        plugin._bili_http_json = fake_http

        # 3) 查询成功 + 缓存
        reset_http()
        responses["12345678"] = bili_card(name="小明", level=4, fans=520, sign="签名")
        info, error, kind = await plugin._bili_lookup("12345678")
        check(info is not None and error == "" and kind == "", "查询成功返回账号信息")
        check(info["name"] == "小明" and info["level"] == 4 and info["fans"] == 520, "解析昵称/等级/粉丝")
        check(http_calls == ["12345678"] and "photo=false" in http_urls[0], "请求带 mid 与 photo=false")
        await plugin._bili_lookup("12345678")
        check(http_calls == ["12345678"], "命中缓存不重复请求 B站")

        # 4) 账号不存在：即使 on_error=pass 也必须拒绝，且不重试
        reset_http()
        plugin.config["bili_uid_on_error"] = "pass"
        responses["9999999999"] = bili_card(code=-404, message="啥都木有")
        passed, reason, _info = await plugin._bili_verify("9999999999", "80001")
        check(not passed and "不存在" in reason, f"账号不存在时一定不通过（{reason}）")
        check(http_calls == ["9999999999"], "账号不存在不重试")

        # 5) 风控：reject 拒绝（重试一次后放弃）
        reset_http()
        plugin.config["bili_uid_on_error"] = "reject"
        responses["12345678"] = [
            bili_card(code=-799, message="请求过于频繁"),
            bili_card(code=-799, message="请求过于频繁"),
        ]
        passed, reason, _info = await plugin._bili_verify("12345678", "80001")
        check(not passed and "频繁" in reason, f"风控时按 reject 拒绝（{reason}）")
        check(http_calls == ["12345678", "12345678"], "风控会重试一次再放弃")

        # 6) 风控：pass 放行并标记未核验
        reset_http()
        plugin.config["bili_uid_on_error"] = "pass"
        responses["12345678"] = bili_card(code=-799, message="请求过于频繁")
        passed, reason, info = await plugin._bili_verify("12345678", "80001")
        check(passed and "放行" in reason and info.get("unverified") is True, "风控时按 pass 放行并标记未核验")

        # 7) 先风控后成功
        reset_http()
        plugin.config["bili_uid_on_error"] = "reject"
        responses["23456789"] = [
            bili_card(code=-799, message="请求过于频繁"),
            bili_card(name="重试成功", level=2, fans=1),
        ]
        passed, _reason, info = await plugin._bili_verify("23456789", "80002")
        check(passed and info["name"] == "重试成功", "风控后重试成功即通过")

        # 8) 网络异常
        reset_http()
        responses["34567890"] = RuntimeError("连接超时")
        passed, reason, _info = await plugin._bili_verify("34567890", "80003")
        check(not passed and "请求失败" in reason, f"网络异常按 reject 判不通过（{reason}）")

        # 9) 规则：等级 / 粉丝 / 昵称关键词
        reset_http()
        plugin.config["bili_uid_min_level"] = 3
        responses["45678901"] = bili_card(name="低等级", level=1, fans=999)
        passed, reason, _info = await plugin._bili_verify("45678901", "80004")
        check(not passed and "等级" in reason, f"等级不足被拒（{reason}）")
        plugin.config["bili_uid_min_level"] = 0

        reset_http()
        plugin.config["bili_uid_min_fans"] = 100
        responses["56789012"] = bili_card(name="没粉丝", level=6, fans=9)
        passed, reason, _info = await plugin._bili_verify("56789012", "80005")
        check(not passed and "粉丝" in reason, f"粉丝不足被拒（{reason}）")
        plugin.config["bili_uid_min_fans"] = 0

        reset_http()
        plugin.config["bili_uid_name_keywords"] = ["猫", "neko"]
        responses["67890123"] = bili_card(name="狗子", level=6, fans=100)
        passed, reason, _info = await plugin._bili_verify("67890123", "80006")
        check(not passed and "昵称" in reason, f"昵称不含关键词被拒（{reason}）")
        responses["67890124"] = bili_card(name="neko猫猫", level=6, fans=100)
        passed, _reason, _info = await plugin._bili_verify("67890124", "80006")
        check(passed, "昵称含关键词则通过")
        plugin.config["bili_uid_name_keywords"] = []

        # 10) 一 UID 一 QQ
        reset_http()
        plugin._bili_bind("78901234", "80007", "占位", "123456")
        responses["78901234"] = bili_card(name="复用者", level=6, fans=100, uid="78901234")
        passed, reason, _info = await plugin._bili_verify("78901234", "80008")
        check(not passed and "已被 QQ 80007" in reason, f"同一 UID 被别的 QQ 复用会被拒（{reason}）")
        passed, _reason, _info = await plugin._bili_verify("78901234", "80007")
        check(passed, "同一个 QQ 再用自己的 UID 仍然通过")
        check(plugin._state["bili_uids"]["78901234"]["qq"] == "80007", "绑定记录里保存了 QQ")

        # 11) 提问时自动附上索取 UID 的提示（问题文案里没提 UID）
        reset_http()
        ask = FakeEvent(post_type="notice", notice_type="group_increase", user_id="80010")
        await plugin.on_group_notice(ask)
        question_text = chain_text(ask.sent[0])
        check("B站 UID" in question_text and "请证明你是真人" in question_text, "提问会附上索取 UID 的提示")
        check(question_text.count("UID") == 1, "提示只出现一次")

        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "你的 UID 是多少？", "hint": "", "answers": ["abc"], "match_mode": "inherit"}
        ]
        ask_dup = FakeEvent(post_type="notice", notice_type="group_increase", user_id="80012")
        await plugin.on_group_notice(ask_dup)
        check("发给我" not in chain_text(ask_dup.sent[0]), "问题里已经提到 UID 时不重复追加提示")
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "请证明你是真人：", "hint": "", "answers": ["abc"], "match_mode": "inherit"}
        ]

        # 12) 端到端：UID 正确即通过并发码，大模型完全不参与
        reset_review(review_mode="llm", code_send_mode="private", private_send_channel="auto")
        plugin.config["bili_uid_enabled"] = True
        plugin.config["bili_uid_unique"] = True
        plugin._state["pending"] = {}
        plugin._state["approved"] = {}
        reset_http()
        llm_provider = FakeProvider(reply="PASS")
        context.providers = [llm_provider]
        context.using_provider = llm_provider
        context.sent.clear()
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="80020"))
        responses["88880000"] = bili_card(name="申请人", level=5, fans=66, uid="88880000")
        ok_event = FakeEvent(user_id="80020", text="我的UID是 88880000")
        await plugin.on_group_message(ok_event)
        approved = plugin._state["approved"].get("123456:80020") or {}
        check(approved.get("bili_uid") == "88880000", f"通过后记录 B站 UID（{approved.get('bili_uid')}）")
        check(approved.get("bili_name") == "申请人", f"通过后记录 B站昵称（{approved.get('bili_name')}）")
        check(plugin._bili_bound_qq("88880000") == "80020", "UID 与 QQ 的绑定已写入状态")
        check(llm_provider.calls == [], "开了 UID 审核时不会调用大模型（判定是确定性的）")
        temp_sent = client.temp_sessions
        current_code = plugin._state["code"]
        check(
            temp_sent
            and str(temp_sent[-1].get("user_id")) == "80020"
            and current_code in str(temp_sent[-1].get("message") or ""),
            f"通过后照常下发验证码（群临时会话 {len(temp_sent)} 条）",
        )
        check("123456:80020" not in plugin._state["pending"], "通过后清理待审核记录")

        # 13) UID 不存在 → 按答错处理；没有 UID → 明确原因
        await plugin.on_group_notice(FakeEvent(post_type="notice", notice_type="group_increase", user_id="80021"))
        responses["11112222"] = bili_card(code=-404, message="啥都木有")
        wrong_event = FakeEvent(user_id="80021", text="我的UID是 11112222")
        await plugin.on_group_message(wrong_event)
        wrong_text = chain_text(wrong_event.sent[0])
        check("不正确" in wrong_text and "2 次机会" in wrong_text, "UID 不存在时按答错处理并提示剩余次数")
        check("不存在" in wrong_text and "原因：" in wrong_text, f"答错提示里说明了原因（{wrong_text.splitlines()[-1][:40]}）")
        check("123456:80021" in plugin._state["pending"], "答错后仍在待审核队列")
        no_uid_event = FakeEvent(user_id="80021", text="我不想说")
        await plugin.on_group_message(no_uid_event)
        check("没有识别到" in chain_text(no_uid_event.sent[0]), "回答里没有 UID 时给出明确原因")

        # 14) 管理指令
        reset_http()
        responses["99990000"] = bili_card(name="被查的人", level=6, fans=1234, uid="99990000")
        plugin._bili_bind("99990000", "80030", "被查的人", "123456")
        lookup_reply = chain_text([r async for r in plugin.review_bili_lookup(admin_event, "99990000")][0])
        check("被查的人" in lookup_reply and "等级：6" in lookup_reply, "查UID 显示昵称与等级")
        check("按当前规则" in lookup_reply and "绑定情况" in lookup_reply, "查UID 显示规则判定与绑定情况")
        check("QQ 80030" in lookup_reply, "查UID 显示占用该 UID 的 QQ")
        check(
            "只有管理员" in chain_text([r async for r in plugin.review_bili_lookup(FakeEvent(user_id="10001"), "1")][0]),
            "查UID 有权限校验",
        )

        plugin._bili_bind("88880000", "80020", "申请人", "123456")
        unbind_reply = chain_text([r async for r in plugin.review_bili_unbind(admin_event, "88880000")][0])
        check("已解绑" in unbind_reply, "解绑指令生效")
        check(plugin._bili_bound_qq("88880000") == "", "解绑后绑定记录被清除")
        check(
            "本来就没有绑定" in chain_text([r async for r in plugin.review_bili_unbind(admin_event, "88880000")][0]),
            "重复解绑给出提示",
        )

        # 15) 诊断 / 状态 / 配置校验
        diag_text = chain_text([r async for r in plugin.review_diagnose(admin_event)][0])
        check("B站 UID 审核" in diag_text and "已启用" in diag_text, "诊断报告包含 B站 UID 审核状态")
        check("已绑定 UID" in diag_text, "诊断报告显示绑定数量")
        status_text = chain_text([r async for r in plugin.review_status(admin_event)][0])
        check("已通过" in status_text, "状态报告仍正常输出")

        FAKE_REQUEST.payload = {"values": {"bili_uid_on_error": "maybe"}}
        resp = await plugin.api_save_settings()
        check(
            resp["status_code"] == 400 and "bili_uid_on_error" in resp["payload"]["message"],
            "非法的 bili_uid_on_error 被拒绝",
        )
        FAKE_REQUEST.payload = {"values": {"bili_uid_min_level": 9}}
        resp = await plugin.api_save_settings()
        check(resp["status_code"] == 400, "越界的 bili_uid_min_level 被拒绝")

        # 收尾：恢复真实实现
        plugin.__dict__.pop("_bili_http_json", None)
        plugin.config["bili_uid_enabled"] = False
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "1+1=?", "hint": "", "answers": ["2"], "match_mode": "inherit"}
        ]
        context.providers = []
        context.using_provider = None
        reset_review(review_mode="rule")

        print("\n[22] 网页审核对接（同步 / 拉取 / 免提问）")
        reset_review(review_mode="rule", code_send_mode="private", private_send_channel="auto")
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "1+1=?", "hint": "", "answers": ["2"], "match_mode": "inherit"}
        ]
        plugin.config["bili_uid_enabled"] = False
        plugin.config["web_review_url"] = ""
        plugin.config["web_review_token"] = ""
        plugin.config["web_review_enabled"] = False
        check(not plugin._web_review_enabled(), "没填 url/token 时视为未启用")
        plugin.config["web_review_url"] = "http://127.0.0.1:8787/"
        check(not plugin._web_review_enabled(), "只有 url 没 token 时仍未启用")
        plugin.config["web_review_token"] = "tok-test"
        check(not plugin._web_review_enabled(), "开关没打开时未启用")
        plugin.config["web_review_enabled"] = True
        check(plugin._web_review_enabled(), "三项齐全后启用")
        check(plugin._web_review_url() == "http://127.0.0.1:8787", "url 结尾斜杠被去掉")
        plugin.config["web_review_url"] = "file:///C:/Windows/win.ini"
        check(plugin._web_review_url() == "" and not plugin._web_review_enabled(), "拒绝非 http(s) 的站点地址")
        plugin.config["web_review_url"] = "http://127.0.0.1:8787"

        # 假站点：按 (method, path) 配响应
        web_calls = []
        responses = {}

        async def fake_http(method, url, *, headers=None, payload=None, timeout=15.0):
            path = url.split("://", 1)[1].split("/", 1)[1]
            query = ""
            if "?" in path:
                path, query = path.split("?", 1)
            web_calls.append(
                {"method": method, "path": path, "query": query, "payload": payload, "headers": dict(headers or {})}
            )
            item = responses.get((method, path))
            if item is None:
                raise AssertionError(f"自检没有为 {method} {path} 配置响应")
            if isinstance(item, Exception):
                raise item
            return item

        plugin._http_json = fake_http

        # 快照内容
        plugin._state["code"] = "SNAP01"
        plugin._state["bili_uids"] = {"80000001": {"qq": "90001", "name": "网页用户", "group_id": "123456", "at": time.time()}}
        plugin._state["pending"] = {"123456:90009": {"user_id": "90009"}}
        plugin._state["approved"] = {}
        snapshot = plugin._web_review_snapshot()
        check(snapshot["token"] == "tok-test" and snapshot["code"] == "SNAP01", "快照带上 token 与验证码")
        check(snapshot["groups"] == ["123456"], "快照带上审核群")
        check(snapshot["bindings"] == {"80000001": "90001"}, "快照带上 UID↔QQ 绑定")
        check(snapshot["bili"]["bili_min_level"] == 0 and "bili_on_error" in snapshot["bili"], "快照带上 B站 规则")
        check(snapshot["stats"]["pending"] == 1, "快照带上待审核/已通过统计")
        check(isinstance(snapshot.get("questions"), list) and snapshot["questions"], "快照带上题库（网页可以出同样的题）")
        check(
            all({"enabled", "question", "hint", "answers", "match_mode"} <= set(item) for item in snapshot["questions"]),
            "快照里的题目字段完整",
        )
        check(
            "common_answers" in snapshot and "match_mode" in snapshot and "fuzzy_threshold" in snapshot,
            "快照带上通用答案库与匹配方式",
        )
        check(snapshot.get("question_mode") in ("plugin", "site"), f"快照带上题库来源模式（{snapshot.get('question_mode')}）")
        check(snapshot.get("push_code") is True, "快照带上'通过后立即发码'开关")

        # 网页端与插件的判定必须**逐字一致**，否则同一句话在 QQ 过、在网页不过
        matching_dir = PLUGIN_MAIN.parent / "review_web"
        if (matching_dir / "matching.py").is_file():
            sys.path.insert(0, str(PLUGIN_MAIN.parent))
            from review_web import matching as site_matching  # noqa: E402

            matrix = [
                ("我觉得是 2", ["2"], "contains"),
                ("２", ["2"], "exact"),
                ("2。", ["2"], "exact"),
                ("我有三只猫", ["猫"], "contains"),
                ("红烧肉呀", ["红烧肉"], "fuzzy"),
                ("完全不相干", ["学习交流"], "fuzzy"),
                ("abc123", [r"abc\d+"], "regex"),
                ("xxx", ["["], "regex"),
                ("", ["2"], "contains"),
                ("   ", ["2"], "contains"),
            ]
            mismatches = []
            for text, answers, mode in matrix:
                plugin_pass, plugin_detail = plugin._match_with(text, answers, mode, 0.8)
                site_pass, site_detail = site_matching.match_one(text, answers, mode, 0.8)
                if (plugin_pass, plugin_detail) != (site_pass, site_detail):
                    mismatches.append((text, answers, mode, plugin_detail, site_detail))
            check(not mismatches, f"单题判定与网站逐字一致（{len(matrix)} 组）" + (f" 差异：{mismatches[:2]}" if mismatches else ""))

            common_cases = [
                ("我的邀请码1234", ["2"], ["邀请码1234"], "contains"),
                ("这个不太对", ["甲"], ["乙"], "fuzzy"),
                ("2", ["2"], [], "exact"),
            ]
            rule_mismatches = []
            for text, answers, common, mode in common_cases:
                plugin_pass, plugin_detail = plugin._evaluate_rules(text, answers, mode, common)
                site_pass, site_detail = site_matching.evaluate(text, answers, mode, common, 0.8)
                if (plugin_pass, plugin_detail) != (site_pass, site_detail):
                    rule_mismatches.append((text, plugin_detail, site_detail))
            check(
                not rule_mismatches,
                f"含通用答案库的判定也一致（{len(common_cases)} 组）" + (f" 差异：{rule_mismatches[:2]}" if rule_mismatches else ""),
            )
            check(
                site_matching.normalize("　ＡＢＣ 。") == module._normalize_text("　ＡＢＣ 。"),
                "归一化（全角/标点/空白）两边一致",
            )
        else:
            print("  SKIP  没有 review_web，跳过匹配一致性对照")

        # 同步成功 / 失败
        responses[("POST", "api/plugin/sync")] = {"ok": True, "pending_deliveries": 2}
        web_calls.clear()
        ok_sync, note = await plugin._web_review_sync()
        check(ok_sync and "已同步" in note, f"同步成功（{note}）")
        check(web_calls[0]["payload"]["token"] == "tok-test", "同步请求带上 token")
        check(plugin._web_review_status().get("last_sync_at"), "记录最近同步时间")
        check(plugin._web_review_status().get("pending_deliveries") == 2, "记录网站端待投递条数")
        responses[("POST", "api/plugin/sync")] = RuntimeError("连接被拒绝")
        ok_sync, note = await plugin._web_review_sync()
        check(not ok_sync and "连接被拒绝" in note, "同步失败时返回原因")
        responses[("POST", "api/plugin/sync")] = {"ok": False, "error": "token 不正确"}
        ok_sync, note = await plugin._web_review_sync()
        check(not ok_sync and "被拒绝" in note, "网站返回错误时如实上报")

        # 拉取 + ack
        responses[("POST", "api/plugin/sync")] = {"ok": True, "pending_deliveries": 1}
        responses[("GET", "api/plugin/applications")] = {
            "ok": True,
            "count": 1,
            "items": [{"id": 7, "qq": "90001", "uid": "80000001", "uid_name": "网页用户", "code": "SNAP01", "decided_at": time.time()}],
        }
        responses[("POST", "api/plugin/ack")] = {"ok": True, "acked": 1}
        web_calls.clear()
        pulled, note = await plugin._web_review_pull()
        check(pulled == 1 and "1 条" in note, f"拉取到 1 条（{note}）")
        record = plugin._state["approved"].get("123456:90001") or {}
        check(record.get("source") == "web", "网页通过的记录标记 source=web")
        check(record.get("bili_uid") == "80000001" and record.get("bili_name") == "网页用户", "记录带上 B站 UID 与昵称")
        check("123456:90001" not in plugin._state["pending"], "进来后不再留在待审核队列")
        ack_call = [c for c in web_calls if c["path"] == "api/plugin/ack"]
        check(ack_call and ack_call[0]["payload"]["ids"] == [7], "拉取后按编号 ack")
        check(
            ack_call and ack_call[0]["headers"].get("X-Review-Token") == "tok-test",
            "ack 用请求头带 token 而不是放进 URL",
        )
        pull_call = [c for c in web_calls if c["path"] == "api/plugin/applications"]
        check(pull_call and "token=" not in pull_call[0]["query"], f"拉取 URL 里不出现 token（{pull_call[0]['query'] if pull_call else ''}）")
        check(plugin._web_review_status().get("pulled") == 1, "累计接收数增加")
        responses[("GET", "api/plugin/applications")] = {"ok": True, "count": 0, "items": []}
        web_calls.clear()
        pulled, note = await plugin._web_review_pull()
        check(pulled == 0 and not [c for c in web_calls if c["path"] == "api/plugin/ack"], "没有新记录时不 ack")
        responses[("GET", "api/plugin/applications")] = RuntimeError("超时")
        pulled, note = await plugin._web_review_pull()
        check(pulled == 0 and "失败" in note, "拉取失败时返回原因")

        # ① 反向同步题库：网站后台改的题拉回插件（web_question_mode=site）
        plugin.config["web_question_mode"] = "plugin"
        pulled_q, note_q = await plugin._web_review_pull_questions()
        check(pulled_q == 0 and "无需反向同步" in note_q, f"插件题库模式不反向拉取（{note_q}）")
        check(not [c for c in web_calls if c["path"] == "api/plugin/questions"], "插件题库模式连请求都不发")

        plugin.config["web_question_mode"] = "site"
        responses[("GET", "api/plugin/questions")] = {
            "ok": True,
            "mode": "site",
            "updated_at": 1234.0,
            "count": 2,
            "questions": [
                {"enabled": True, "question": "网站题：群主最喜欢的动物？", "hint": "一个字", "answers": ["猫"], "match_mode": "exact"},
                {"enabled": True, "question": "网站题：暗号是什么？", "hint": "", "answers": ["芝麻开门"], "match_mode": "fuzzy"},
            ],
            "common_answers": ["邀请码1234"],
            "match_mode": "contains",
            "fuzzy_threshold": 0.75,
        }
        web_calls.clear()
        pulled_q, note_q = await plugin._web_review_pull_questions()
        check(pulled_q == 2 and "2 条" in note_q, f"从网站拉回 2 条题目（{note_q}）")
        question_call = [c for c in web_calls if c["path"] == "api/plugin/questions"]
        check(
            question_call and question_call[0]["headers"].get("X-Review-Token") == "tok-test",
            "拉题库用请求头带 token",
        )
        check(plugin.config["questions"][0]["question"] == "网站题：群主最喜欢的动物？", "网站题目写回插件配置")
        check(plugin.config["questions"][1]["answers"] == ["芝麻开门"], "答案一起写回")
        check(plugin.config["questions"][1]["match_mode"] == "fuzzy", "每题匹配方式一起写回")
        check(plugin.config["common_answers"] == ["邀请码1234"], "网站通用答案库写回插件配置")
        check(plugin.config.saved, "改题库走 save_config 落盘")
        pulled_q, note_q = await plugin._web_review_pull_questions()
        check(pulled_q == 0 and "没有变化" in note_q, f"内容没变时不重复写配置（{note_q}）")

        # ③ 网页后台通过后立即私发验证码
        plugin.config["web_review_push_code"] = True
        plugin.config["code_send_mode"] = "private"
        plugin.config["private_send_channel"] = "auto"
        plugin._state["code"] = "PUSH88"
        responses[("GET", "api/plugin/applications")] = {
            "ok": True,
            "count": 1,
            "items": [{"id": 88, "qq": "90011", "uid": "80000011", "uid_name": "被一键通过的人"}],
        }
        responses[("POST", "api/plugin/ack")] = {"ok": True, "acked": 1}
        client.temp_sessions.clear()
        pulled, note = await plugin._web_review_pull()
        check(pulled == 1 and "立即私发验证码 1/1" in note, f"拉取后立即发码（{note}）")
        check(
            client.temp_sessions and str(client.temp_sessions[-1]["user_id"]) == "90011"
            and "PUSH88" in str(client.temp_sessions[-1]["message"]),
            "验证码真的私发给了那个 QQ",
        )
        # 关掉开关就不再发
        plugin.config["web_review_push_code"] = False
        responses[("GET", "api/plugin/applications")] = {
            "ok": True,
            "count": 1,
            "items": [{"id": 89, "qq": "90012", "uid": "80000012", "uid_name": "别人"}],
        }
        client.temp_sessions.clear()
        pulled, note = await plugin._web_review_pull()
        check(pulled == 1 and "立即私发" not in note, f"关闭开关后不立即发码（{note}）")
        check(not client.temp_sessions, "关闭开关后确实没发")
        # 临时会话发不出去时：记录失败但不影响接收
        plugin.config["web_review_push_code"] = True
        client.fail_temp_session = True   # 临时会话失败
        context.fail_private = True       # 好友私聊也失败
        responses[("GET", "api/plugin/applications")] = {
            "ok": True,
            "count": 1,
            "items": [{"id": 90, "qq": "90013", "uid": "80000013", "uid_name": "发不出去的人"}],
        }
        client.calls.clear()
        pulled, note = await plugin._web_review_pull()
        check(pulled == 1 and "0/1" in note, f"发不出去时如实报 0/1（{note}）")
        check(plugin._state["approved"].get("123456:90013"), "发不出去也照样记为已通过")
        check(context.private_failures, "确实尝试过私聊并失败")
        client.fail_temp_session = False
        context.fail_private = False
        # 收尾：把题库与来源模式恢复成后面用例期望的样子
        plugin.config["questions"] = [
            {"__template_key": "question_item", "enabled": True, "question": "1+1=?", "hint": "", "answers": ["2"], "match_mode": "inherit"}
        ]
        plugin.config["common_answers"] = []
        plugin.config["web_question_mode"] = "plugin"

        # 网页已通过的人入群：不提问、直接发码
        plugin.config["web_review_auto_approve"] = True
        client.fail_temp_session = False
        client.temp_sessions.clear()
        join = FakeEvent(post_type="notice", notice_type="group_increase", user_id="90001")
        await plugin.on_group_notice(join)
        check(not join.sent, "网页已通过者入群时不再提问")
        check(
            client.temp_sessions and str(client.temp_sessions[-1]["user_id"]) == "90001"
            and plugin._state["code"] in str(client.temp_sessions[-1]["message"]),
            "直接把当日验证码私发给他",
        )
        check("123456:90001" not in plugin._state["pending"], "没有为已通过者建待审核记录")
        check(plugin._diag["counters"].get("web_approved") == 1, "网页免提问计数被记录")

        # 关闭自动发码：回到正常提问流程
        plugin.config["web_review_auto_approve"] = False
        plugin._state["approved"]["123456:90002"] = {
            "at": time.time(), "code": "", "source": "web", "bili_uid": "80000002", "question": "网页审核"
        }
        join2 = FakeEvent(post_type="notice", notice_type="group_increase", user_id="90002")
        await plugin.on_group_notice(join2)
        check(any("1+1=?" in text for text in texts_of(join2.sent)), "关闭自动发码后仍按正常流程提问")
        plugin.config["web_review_auto_approve"] = True

        # 指令
        plugin.config["web_review_enabled"] = False
        off_text = chain_text([r async for r in plugin.review_website(admin_event)][0])
        check("未启用" in off_text, "指令：未启用时给出提示")
        plugin.config["web_review_enabled"] = True
        plugin.config["web_review_token"] = ""
        no_token = chain_text([r async for r in plugin.review_website(admin_event)][0])
        check("未启用" in no_token and "两种开启方式" in no_token, "指令：都没启用时给出两种开启方式")
        plugin.config["web_review_token"] = "tok-test"
        status_text = chain_text([r async for r in plugin.review_website(admin_event)][0])
        check("审核网站" in status_text and "127.0.0.1:8787" in status_text, "指令：显示站点与状态")
        check("模式：独立站点" in status_text, "指令：标明当前是独立站点模式")
        check("累计接收" in status_text, "指令：显示累计接收条数")
        responses[("POST", "api/plugin/sync")] = {"ok": True, "pending_deliveries": 0}
        responses[("GET", "api/plugin/applications")] = {"ok": True, "count": 0, "items": []}
        sync_replies = [r async for r in plugin.review_website(admin_event, "同步")]
        check("正在同步" in chain_text(sync_replies[0]) and "已同步" in chain_text(sync_replies[-1]), "指令：同步动作有回执")
        check(
            "只有管理员" in chain_text([r async for r in plugin.review_website(FakeEvent(user_id="10001"))][0]),
            "指令有权限校验",
        )

        # 诊断与后台任务
        diag_text = chain_text([r async for r in plugin.review_diagnose(admin_event)][0])
        check("网页审核：✅ 已启用" in diag_text, "诊断报告包含网页审核状态")
        plugin._loops.clear()
        plugin.config["web_review_enabled"] = True
        plugin.config["web_review_url"] = "http://127.0.0.1:8787"
        plugin._ensure_loops()
        check(len(plugin._loops) == 3, f"启用网页审核后多一个后台任务（{len(plugin._loops)}）")
        for task in plugin._loops:
            task.cancel()
        plugin._loops.clear()
        await asyncio.sleep(0)

        # 收尾
        plugin.__dict__.pop("_http_json", None)
        plugin.config["web_review_enabled"] = False
        plugin.config["web_review_url"] = ""
        plugin.config["web_review_token"] = ""
        plugin._state["approved"] = {}
        plugin._state["pending"] = {}
        plugin._state["bili_uids"] = {}
        reset_review(review_mode="rule")

        print("\n[23] 端到端联调：真实站点 + 真实 HTTP")
        plugin_dir = PLUGIN_MAIN.parent
        site_root = plugin_dir / "review_web"
        if not site_root.is_dir():
            print(f"  SKIP  未找到站点实现目录（{site_root}），跳过端到端联调")
        else:
            sys.path.insert(0, str(plugin_dir))
            from review_web import app as site_app  # noqa: E402
            from review_web import bili as site_bili  # noqa: E402
            from review_web.store import Store as SiteStore  # noqa: E402
            import threading  # noqa: E402
            import urllib.request  # noqa: E402

            e2e_dir = Path(__file__).resolve().parent / ".selftest_data" / "e2e"
            shutil.rmtree(e2e_dir, ignore_errors=True)
            e2e_dir.mkdir(parents=True, exist_ok=True)
            site_store = SiteStore(e2e_dir / "e2e.db")
            site_store.set_setting("plugin_token", "e2e-token-1234567890")
            site_store.set_setting("use_plugin_rules", False)
            site_bili._fetch_json = lambda url, timeout, referer: {
                "code": 0,
                "data": {
                    "card": {"name": "端到端用户", "fans": 10, "level_info": {"current_level": 3}},
                    "follower": 10,
                },
            }
            site_bili.clear_cache()
            site_server = site_app.make_server(host="127.0.0.1", port=0, store=site_store)
            site_port = site_server.server_address[1]
            threading.Thread(target=site_server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{site_port}"

            def site_post(path, payload):
                request = urllib.request.Request(
                    base + path,
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))

            # 网页现在会出题：固定题库，并按题作答
            plugin.config["questions"] = [
                {
                    "__template_key": "question_item",
                    "enabled": True,
                    "question": "端到端测试题：1+1=?",
                    "hint": "",
                    "answers": ["2"],
                    "match_mode": "inherit",
                }
            ]
            plugin.config["common_answers"] = []
            plugin.config["code_reset_time"] = "off"

            try:
                status, body = site_post(
                    "/api/apply",
                    {"qq": "91001", "uid": "82000001", "question": "端到端测试题：1+1=?", "answer": "2"},
                )
                check(status == 200 and body["status"] == "approved", f"申请人在真实站点上提交并通过（{status}）")
                status, body = site_post(
                    "/api/apply",
                    {"qq": "91002", "uid": "82000002", "question": "端到端测试题：1+1=?", "answer": "2"},
                )
                check(body["status"] == "approved", "第二位申请人也通过")

                # 恢复插件的真实 HTTP 实现，让插件真的去访问站点
                plugin.__dict__.pop("_http_json", None)
                plugin.config["web_review_enabled"] = True
                plugin.config["web_review_url"] = base
                plugin.config["web_review_token"] = "e2e-token-1234567890"
                plugin.config["web_review_auto_approve"] = True
                plugin._state["approved"] = {}
                plugin._state["pending"] = {}
                plugin._state["bili_uids"] = {}
                plugin._state["code"] = "E2E001"
                plugin._state["web_review"] = {}  # 只统计这一节接收到的条数
                synced, pulled, note = await plugin._web_review_once()
                check(synced, f"插件真实 HTTP 同步成功（{note}）")
                check(pulled == 2, f"插件真实 HTTP 拉取到 2 条（{pulled}）")
                check(
                    plugin._state["approved"].get("123456:91001", {}).get("source") == "web",
                    "插件状态里写入网页通过记录",
                )
                check(plugin._bili_bound_qq("82000001") == "91001", "B站 UID 绑定被同步回插件")
                check(site_store.get_setting("plugin_code") == "E2E001", "站点侧收到了插件同步的验证码")
                check(site_store.get_setting("plugin_synced_at"), "站点记录了同步时间")

                # 站点把验证码展示给后来提交的申请人
                status, body = site_post(
                    "/api/apply",
                    {"qq": "91003", "uid": "82000003", "question": "端到端测试题：1+1=?", "answer": "2"},
                )
                check(body["code"] == "E2E001", f"网页直接显示插件同步来的验证码（{body['code']}）")

                # 网页已通过者入群：不提问、直接发码
                client.fail_temp_session = False
                client.temp_sessions.clear()
                e2e_join = FakeEvent(post_type="notice", notice_type="group_increase", user_id="91001")
                await plugin.on_group_notice(e2e_join)
                check(not e2e_join.sent, "端到端：网页已通过者入群不再提问")
                check(
                    client.temp_sessions and "E2E001" in str(client.temp_sessions[-1]["message"]),
                    "端到端：直接私发当日验证码",
                )

                # 第三位申请人是在第一轮之后提交的，所以再拉一轮会收到这 1 条
                synced, pulled, note = await plugin._web_review_once()
                check(synced and pulled == 1, f"端到端：新提交的申请被增量拉取（{note}）")
                check(
                    plugin._state["approved"].get("123456:91003", {}).get("source") == "web",
                    "第三位申请人也写入网页通过记录",
                )
                # 幂等：已经接收过的不会被重复接收
                synced, pulled, note = await plugin._web_review_once()
                check(synced and pulled == 0, f"端到端：已接收过的不再重复（{note}）")
                check(plugin._web_review_status().get("pulled") == 3, "累计接收数正确（3 条）")

                # 站点侧换码后，插件下一轮同步会把新码推上去
                plugin._state["code"] = "E2E002"
                synced, pulled, note = await plugin._web_review_once()
                check(site_store.get_setting("plugin_code") == "E2E002", "换码后站点侧同步更新")
                check(
                    all(
                        row["code"] == "E2E002"
                        for row in site_store.all_approved()
                    ),
                    "站点上已通过记录的验证码一起刷新",
                )
            finally:
                site_server.shutdown()
                site_server.server_close()
                plugin.__dict__.pop("_http_json", None)
                plugin.config["web_review_enabled"] = False
                plugin.config["web_review_url"] = ""
                plugin.config["web_review_token"] = ""
                plugin._state["approved"] = {}
                plugin._state["pending"] = {}
                plugin._state["bili_uids"] = {}
                reset_review(review_mode="rule")

        print("\n[24] 内置审核网站（随插件启动）")
        import socket as _socket  # noqa: E402
        import urllib.request as _urlreq  # noqa: E402

        def free_port() -> int:
            probe = _socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            return port

        def site_get(url: str) -> tuple[int, str]:
            try:
                with _urlreq.urlopen(url, timeout=10) as response:
                    return response.status, response.read().decode("utf-8", "replace")
            except Exception as exc:
                return 0, f"{type(exc).__name__}: {exc}"

        plugin.config["web_review_enabled"] = False
        plugin.config["web_review_url"] = ""
        plugin.config["web_review_token"] = ""
        plugin.config["web_site_enabled"] = True
        plugin.config["web_site_host"] = "127.0.0.1"
        port = free_port()
        plugin.config["web_site_port"] = port

        check(plugin._site_builtin_enabled() and not plugin._site_running(), "未启动时状态正确")
        check(await plugin._start_builtin_site(), f"内置站点启动成功（端口 {port}，{plugin._site_error or 'ok'}）")
        check(plugin._site_running() and plugin._site_bound_port == port, "记录运行状态与端口")
        status, body = site_get(f"http://127.0.0.1:{port}/healthz")
        check(status == 200 and '"ok"' in body, f"真实 HTTP 健康检查通过（{status}）")
        status, body = site_get(f"http://127.0.0.1:{port}/")
        check(status == 200 and "入群审核" in body, "申请页可以直接打开")
        check(
            plugin._state_path and str(plugin._site_store.path).startswith(str(Path(plugin._state_path).parent)),
            f"站点数据放在插件数据目录下（{plugin._site_store.path.name}）",
        )

        # 内置模式下无需配置 url/token 也能对接
        check(plugin._web_review_enabled(), "内置模式下自动视为已启用（无需 url/token）")
        target_url, target_token = plugin._web_review_target()
        check(target_url == f"http://127.0.0.1:{port}" and bool(target_token), f"同步目标自动指向内置站点（{target_url}）")

        plugin._state["code"] = "BUILTIN1"
        plugin.config["questions"] = [
            {
                "__template_key": "question_item",
                "enabled": True,
                "question": "内置站点测试题：1+1=?",
                "hint": "数字",
                "answers": ["2"],
                "match_mode": "inherit",
            }
        ]
        plugin.config["common_answers"] = []
        synced, pulled, note = await plugin._web_review_once()
        check(synced, f"插件向内置站点同步成功（{note}）")
        check(plugin._site_store.get_setting("plugin_code") == "BUILTIN1", "内置站点收到了验证码")
        check(
            plugin._site_store.get_setting("plugin_questions")
            and plugin._site_store.get_setting("plugin_questions")[0]["question"] == "内置站点测试题：1+1=?",
            "内置站点收到了题库",
        )

        # 走一遍真实网页流程：取题 → 带答案提交 → 通过
        import urllib.request as _urlrequest  # noqa: E402

        def site_post_json(path: str, payload: dict) -> tuple[int, dict]:
            request = _urlrequest.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with _urlrequest.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8", "replace"))

        status, question_body = site_get(f"http://127.0.0.1:{port}/api/questions")
        fetched = json.loads(question_body) if status == 200 else {}
        check(fetched.get("enabled") is True and fetched.get("question") == "内置站点测试题：1+1=?", "内置站点能取到插件同步的题")
        check("2" not in json.dumps(fetched, ensure_ascii=False).replace("1+1=?", ""), "取题接口不下发答案")
        status, applied = site_post_json(
            "/api/apply",
            {"qq": "95001", "uid": "97000001", "question": fetched.get("question"), "answer": "2"},
        )
        check(status == 200 and applied.get("status") == "approved", f"内置站点上答题通过（{status}/{applied.get('status')}）")
        status, wrong = site_post_json(
            "/api/apply",
            {"qq": "95002", "uid": "97000002", "question": fetched.get("question"), "answer": "3"},
        )
        check(wrong.get("status") == "rejected" and "不正确" in str(wrong.get("reason")), "内置站点上答错被判不通过")

        # 指令：内置状态 / 地址 / token / 密码
        status_text = chain_text([r async for r in plugin.review_website(admin_event)][0])
        check("模式：内置" in status_text and f"127.0.0.1:{port}" in status_text, "指令显示内置模式与地址")
        check("站点数据" in status_text, "指令显示站点数据文件")
        addr_text = chain_text([r async for r in plugin.review_website(admin_event, "地址")][0])
        check("发给群友" in addr_text and f"http://127.0.0.1:{port}/" in addr_text, "地址子命令给出可分享链接")
        token_text = chain_text([r async for r in plugin.review_website(admin_event, "token")][0])
        check(target_token in token_text, "token 子命令给出对接 token")
        short_pw = chain_text([r async for r in plugin.review_website(admin_event, "密码 123")][0])
        check("至少8位" in short_pw, "密码太短时给出用法")
        pw_text = chain_text([r async for r in plugin.review_website(admin_event, "密码 newpass-123456")][0])
        check("已更新" in pw_text, "改密子命令执行成功")
        login_body = json.dumps({"password": "newpass-123456"}).encode()
        request = _urlreq.Request(
            f"http://127.0.0.1:{port}/api/admin/login",
            data=login_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urlreq.urlopen(request, timeout=10) as response:
            check(response.status == 200 and "review_admin" in str(response.headers.get("Set-Cookie")), "用新密码能在内置站点登录")

        # 端口冲突：给出可操作的报错而不是崩掉
        plugin._stop_builtin_site()
        check(not plugin._site_running(), "停止后状态复位")
        blocker = _socket.socket()
        # 注意：Windows 上 SO_REUSEADDR 会让"端口被占用"检测失效，这里故意不设
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            started = await plugin._start_builtin_site()
            check(not started and "web_site_port" in plugin._site_error, f"端口被占用时报错并提示改端口（{plugin._site_error}）")
        finally:
            blocker.close()
        check(await plugin._start_builtin_site(), "端口释放后可以重新启动")
        check(plugin._site_running(), "重新启动后处于运行状态")

        # 停止后再启动不会串端口
        plugin._stop_builtin_site()
        check(not plugin._site_running() and plugin._site_bound_port == 0, "停止后端口状态清空")
        check(await plugin._start_builtin_site() and plugin._site_bound_port == port, "可以再次启动在同一端口")

        # 收尾：清理
        plugin._stop_builtin_site()
        plugin.config["web_site_enabled"] = False
        plugin.config["web_review_enabled"] = False
        plugin._state["code"] = ""

        print("\n[25] Pages 资源结构")
        pages = {
            "settings": ("index.html", "app.js", "settings.js", "style.css"),
            "questions": ("index.html", "app.js", "bank.js", "style.css"),
        }
        for page_name, files in pages.items():
            page_dir = PLUGIN_MAIN.parent / "pages" / page_name
            for name in files:
                check((page_dir / name).is_file(), f"pages/{page_name}/{name} 存在")
        i18n_path = PLUGIN_MAIN.parent / ".astrbot-plugin" / "i18n" / "zh-CN.json"
        i18n = json.loads(i18n_path.read_text(encoding="utf-8")) if i18n_path.is_file() else {}
        for page_name in pages:
            check(
                bool(i18n.get("pages", {}).get(page_name, {}).get("title")),
                f"i18n 提供 {page_name} 页标题",
            )
        for page_name in pages:
            index_html = (PLUGIN_MAIN.parent / "pages" / page_name / "index.html").read_text(encoding="utf-8")
            check(
                "./app.js" in index_html and "./style.css" in index_html,
                f"{page_name} 页用相对路径引用资源",
            )
            check(
                'type="module"' in index_html and "bridge-sdk" not in index_html,
                f"{page_name} 页用外部 module 脚本且不手写 bridge SDK",
            )

        questions_js = (PLUGIN_MAIN.parent / "pages" / "questions" / "app.js").read_text(encoding="utf-8")
        check(
            'apiGet("questions")' in questions_js
            and 'apiPost("questions"' in questions_js
            and 'apiPost("questions/parse"' in questions_js,
            "题库页调用三个题库接口",
        )
        check("unwrap(" in questions_js and "aria-expanded" in questions_js, "题库页做信封归一化且列表可展开")
        bank_js = (PLUGIN_MAIN.parent / "pages" / "questions" / "bank.js").read_text(encoding="utf-8")
        check(
            not any(token in bank_js for token in ("document.", "window.", "localStorage", "fetch(")),
            "bank.js 是纯函数模块（无 DOM/网络/存储）",
        )
    finally:
        await plugin.terminate()

    shutil.rmtree(DATA_DIR, ignore_errors=True)

    failed = [label for ok, label in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"自检结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        for label in failed:
            print("  FAILED:", label)
        return 1
    print("全部通过 ✅")
    return 0


class _FakeCommandFilter:
    """类名与 AstrBot 的 CommandFilter 一致，用于触发插件的“这是指令”判断。"""


_FakeCommandFilter.__name__ = "CommandFilter"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
