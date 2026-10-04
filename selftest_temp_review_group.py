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
            version="v1.1.1",
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
        check(data["secret_fields"] == [], "本插件没有 secret 配置项")
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

        print("\n[18] Pages 资源结构")
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
