"""答案匹配（网页端实现）。

**必须与插件里的判定逐字一致**：插件侧在 `main.py` 的
`_normalize_text` / `_match_with` / `_evaluate_rules`，这里按同样的规则、同样的原因文案实现，
插件自检里有一条"两边判定结果完全一致"的对照用例（parity test），防止哪天改歪一边。

匹配方式：
- `regex`：用原始文本 `re.search(pattern, text)`（不归一化），非法正则忽略
- `exact`：归一化后完全相等
- `contains`：归一化后子串包含
- `fuzzy`：先按 contains，再算 `difflib` 相似度，达到阈值即通过
- `inherit`：由调用方解析成全局 match_mode 后传进来（本模块不处理 inherit）
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

# 归一化时剔除的标点与空白（全角会被 NFKC 先转成半角）
PUNCTUATION = set(
    " \t\r\n\u3000!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    "，。、；：？！“”‘’（）【】《》〈〉…—～·￥"
)

MATCH_MODES = ("contains", "exact", "regex", "fuzzy")
MATCH_MODE_CHOICES = ("inherit",) + MATCH_MODES
DEFAULT_MATCH_MODE = "contains"
DEFAULT_FUZZY_THRESHOLD = 0.8


def normalize(text: str) -> str:
    """全角转半角、转小写、去掉空白与标点（与插件 _normalize_text 一致）。"""
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(ch for ch in value if ch not in PUNCTUATION)


def clamp_threshold(value: Any, default: float = DEFAULT_FUZZY_THRESHOLD) -> float:
    """模糊阈值限制在 0.5~1.0（与插件 _fuzzy_threshold 一致）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(1.0, max(0.5, number))


def resolve_mode(mode: Any, global_mode: str = DEFAULT_MATCH_MODE) -> str:
    """把 inherit / 空值解析成具体匹配方式。"""
    text = str(mode or "inherit").strip().lower()
    if text in MATCH_MODES:
        return text
    fallback = str(global_mode or DEFAULT_MATCH_MODE).strip().lower()
    return fallback if fallback in MATCH_MODES else DEFAULT_MATCH_MODE


def match_one(text: str, answers: list[str], mode: str, threshold: float = DEFAULT_FUZZY_THRESHOLD) -> tuple[bool, str]:
    """单组答案的匹配，返回（是否命中, 可读原因）。文案与插件完全一致。"""
    keys = [str(answer).strip() for answer in answers if str(answer).strip()]
    if not keys:
        return False, "该题为空答案"
    if mode == "regex":
        for pattern in keys:
            try:
                if re.search(pattern, text):
                    return True, f"正则 {pattern!r} 命中"
            except re.error:
                continue  # 非法正则忽略（插件里会记日志，网页端静默跳过）
        return False, "没有匹配的正则"
    normalized_text = normalize(text)
    if not normalized_text:
        return False, "回答为空"
    normalized_keys = [(key, normalize(key)) for key in keys]
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


def evaluate(
    text: str,
    answers: list[str],
    mode: str,
    common: list[str] | None = None,
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> tuple[bool, str]:
    """先判该题答案库，再判通用答案库。返回（是否通过, 原因）。"""
    passed, detail = match_one(text, answers, mode, threshold)
    if passed:
        return True, detail
    if common:
        common_passed, common_detail = match_one(text, common, mode, threshold)
        if common_passed:
            return True, f"通用答案库：{common_detail}"
    return False, detail


def explain(
    text: str,
    answers: list[str],
    mode: str,
    common: list[str] | None = None,
    threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> list[str]:
    """逐条列出命中情况（后台调试用）。"""
    lines = [f"匹配方式：{mode} ｜ 归一化后：{normalize(text) or '（空）'}"]
    for label, group in (("题目答案", list(answers or [])), ("通用答案", list(common or []))):
        if not group:
            continue
        lines.append(f"{label}库：{'、'.join(str(item) for item in group)}")
        for answer in group:
            one_passed, detail = match_one(text, [str(answer)], mode, threshold)
            lines.append(f"  {'✅' if one_passed else '❌'} {answer} → {detail}")
    return lines


def question_usable(entry: dict[str, Any], common: list[str] | None = None) -> bool:
    """题目可用 = 有题干，且（该题有答案 或 配了通用答案库）。与插件 _question_usable 一致。"""
    return bool(str(entry.get("question") or "").strip()) and bool(entry.get("answers") or common)
