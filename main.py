"""AstrBot 插件：表情包文字生成。

给 LLM 提供「在固定模板图的左上角气泡里画 ≤8 字文案并发送」的能力，
并提供两条触发路径：

- 显式工具路径：LLM 调用 make_meme 工具，自己给文案与情感分类；
- 概率拦截路径：按配置概率，把 LLM 的回复草稿交给 LLM 浓缩成 ≤8 字
  文案并判断情感，本轮只发图不发文字。

成图会按情感分类归档到 meme_manager 的表情库目录（文件级松耦合，
不依赖其私有 API；目录不存在时跳过归档，不影响发送）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import shutil
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)

from .renderer import render_meme, resolve_font_path

PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = PLUGIN_DIR / "templates" / "template.jpg"

# 与 meme_manager 默认分类对齐；归档目录按此白名单校验
EMOTION_ALLOWLIST = (
    "angry",
    "happy",
    "sad",
    "surprised",
    "confused",
    "color",
    "cpu",
    "fool",
    "givemoney",
    "like",
    "see",
    "shy",
    "work",
    "reply",
    "meow",
    "baka",
    "morning",
    "sleep",
    "sigh",
    "other",
)

DEFAULT_CONFIG = {
    "enable_llm_tool": True,
    "enable_probability": True,
    "group_whitelist": [],
    "probability": 0.2,
    "max_chars": 8,
    "max_text_len": 30,
    "template_path": [],
    "text_box_x": 0.08,
    "text_box_y": 0.11,
    "text_box_w": 0.44,
    "text_box_h": 0.24,
    "bold": True,
    "text_color": "#000000",
    "stroke_color": "",
    "stroke_width": 0,
    "max_output_size": 720,
    "font_path": "",
    "meme_manager_memes_dir": "",
}

# 结果装饰阶段：priority 越大越先执行，这里用极低值保证「最后执行」，
# 从而整体覆盖本轮的最终消息链（压制 meme_manager 等插件插入的图）。
DECORATE_PRIORITY_LAST = -10000

_MEME_MARKER_RE = re.compile(r"\[meme:([A-Za-z0-9_\-=]+)\]")


def parse_summarize_output(
    response_text: str, max_chars: int, allowlist: tuple[str, ...]
) -> tuple[str, str]:
    """解析 LLM 浓缩/情感分类的 JSON 输出。

    Args:
        response_text: LLM 返回文本。
        max_chars: 文案最大字数。
        allowlist: 合法情感分类集合。

    Returns:
        (浓缩文案, 情感分类)；解析失败时返回 ("", "other")。
    """
    raw = (response_text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
    slogan = str(data.get("slogan") or "").strip()
    emotion = str(data.get("emotion") or "").strip().lower()
    if emotion not in allowlist:
        emotion = "other"
    if not slogan:
        return "", "other"
    if len(slogan) > max_chars:
        slogan = slogan[:max_chars]
    return slogan, emotion


def resolve_template_path(configured, plugin_data_root=None) -> str | None:
    """把配置里的模板路径解析为实际存在的文件路径。

    WebUI 的 file 类型配置存的是相对插件数据目录的路径列表（如
    files/template_path/x.jpg），同时兼容旧的绝对路径字符串。

    Args:
        configured: 配置值（字符串或字符串列表）。
        plugin_data_root: 插件数据根目录；不传时按
            get_astrbot_plugin_data_path()/<插件名> 推算。

    Returns:
        存在的模板文件绝对路径；无可用配置时返回 None（调用方回退内置模板）。
    """
    values = (
        configured
        if isinstance(configured, list)
        else ([configured] if configured else [])
    )
    root = (
        Path(plugin_data_root)
        if plugin_data_root
        else Path(get_astrbot_plugin_data_path())
    )
    for value in reversed([str(v) for v in values if str(v).strip()]):
        path = Path(value)
        if not path.is_absolute():
            path = root / value
        if path.is_file():
            return str(path)
    return None


def resolve_memes_base_dir(
    configured: str = "", data_root: Path | None = None
) -> Path | None:
    """解析 meme_manager 表情库根目录（含各情感分类子目录）。

    解析顺序：配置值 → packs 布局（plugin_data/meme_manager/packs/*/memes，
    meme_manager 4.x）→ 扁平布局（plugin_data/meme_manager/memes，3.x）→
    旧版 memes_data。目录均不可用时返回 None（调用方只发送不归档）。

    Args:
        configured: 配置里指定的目录路径，非空且存在时优先。
        data_root: AstrBot 数据根目录；不传时自动获取。

    Returns:
        表情库根目录，或 None。
    """
    if data_root is None:
        data_root = Path(get_astrbot_data_path())
    if configured:
        candidate = Path(configured)
        if candidate.is_dir():
            return candidate
    meme_root = data_root / "plugin_data" / "meme_manager"
    packs_root = meme_root / "packs"
    if packs_root.is_dir():
        candidates = sorted(
            (p for p in packs_root.iterdir() if (p / "memes").is_dir()),
            key=lambda p: p.name != "builtin-default",
        )
        if candidates:
            return candidates[0] / "memes"
    flat = meme_root / "memes"
    if flat.is_dir():
        return flat
    legacy = data_root / "memes_data"
    if legacy.is_dir():
        return legacy
    return None


class MemeTextPlugin(Star):
    """表情包文字生成插件。

    给 LLM 提供「在固定模板图的左上角气泡里画 ≤8 字文案并发送」的能力：
    - 显式工具路径：调用 make_meme 工具，自己给文案与情感分类；
    - 概率拦截路径：按概率把回复草稿交给 LLM 浓缩成 ≤8 字并判断情感，只发图。
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        logger.info("MemeTextPlugin 初始化完成")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _cfg(self, key: str):
        """读取配置，缺失时用默认值。"""
        return self.config.get(key, DEFAULT_CONFIG[key])

    def _template_path(self) -> str:
        """返回模板图路径：优先 WebUI 上传/配置值，其次插件内置模板。"""
        resolved = resolve_template_path(self._cfg("template_path"))
        return resolved or str(DEFAULT_TEMPLATE)

    def _text_box(self) -> dict[str, float]:
        """返回文字框（相对坐标）。"""
        return {
            "x": float(self._cfg("text_box_x")),
            "y": float(self._cfg("text_box_y")),
            "w": float(self._cfg("text_box_w")),
            "h": float(self._cfg("text_box_h")),
        }

    def _render_once(self, text: str, emotion: str) -> dict:
        """渲染一张表情包并把成图归档到表情库。

        Args:
            text: 文案。
            emotion: 情感分类（用于归档目录）。

        Returns:
            {"ok": bool, "path": str, "file_id": str, "err": str | None}。
            以 base64 编码的绝对路径作为 file_id，供 [meme:file_id] 标记使用。
        """
        try:
            font_path = resolve_font_path(str(self._cfg("font_path") or ""))
        except FileNotFoundError as err:
            return {"ok": False, "path": "", "file_id": "", "err": str(err)}
        temp_dir = Path(get_astrbot_temp_path())
        temp_dir.mkdir(parents=True, exist_ok=True)
        out_path = (
            temp_dir
            / f"meme_text_{int(time.time() * 1000)}_{random.randint(10000, 99999)}.png"
        )
        try:
            render_meme(
                self._template_path(),
                text,
                box=self._text_box(),
                font_path=font_path,
                color=str(self._cfg("text_color")),
                stroke_color=str(self._cfg("stroke_color") or ""),
                stroke_width=int(self._cfg("stroke_width") or 0),
                out_path=str(out_path),
                bold=bool(self._cfg("bold")),
                max_size=int(self._cfg("max_output_size") or 0),
            )
        except Exception as err:  # noqa: BLE001 - 渲染失败不应影响正常回复
            logger.error(f"表情包渲染失败: {err}")
            return {"ok": False, "path": "", "file_id": "", "err": str(err)}
        archived = self._archive(out_path, emotion)
        if not archived:
            logger.debug("未归档表情包（meme_manager 图库目录不可用）")
        file_id = base64.urlsafe_b64encode(os.fsencode(str(out_path))).decode()
        return {"ok": True, "path": str(out_path), "file_id": file_id, "err": None}

    def _archive(self, rendered: Path, emotion: str) -> bool:
        """把成图复制一份到 meme_manager 图库的情绪分类目录。

        Args:
            rendered: 渲染出的 PNG 路径。
            emotion: 情感分类。

        Returns:
            是否归档成功；图库目录不可用时返回 False（不阻断发送）。
        """
        base_dir = self._resolve_memes_base_dir()
        if base_dir is None:
            return False
        category = emotion if emotion in EMOTION_ALLOWLIST else "other"
        dest_dir = base_dir / category
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = (
                dest_dir
                / f"meme_text_{int(time.time() * 1000)}_{random.randint(10000, 99999)}.png"
            )
            shutil.copy2(rendered, dest)
            logger.info(f"表情包已归档到 {dest}")
            return True
        except Exception as err:  # noqa: BLE001 - 归档失败只影响存档不影响发送
            logger.error(f"表情包归档失败: {err}")
            return False

    def _resolve_memes_base_dir(self) -> Path | None:
        """确定 meme_manager 表情库根目录（含各情感分类子目录）。"""
        configured = str(self._cfg("meme_manager_memes_dir") or "").strip()
        if configured and not Path(configured).is_dir():
            logger.warning(f"配置的 meme_manager_memes_dir 不存在: {configured}")
        return resolve_memes_base_dir(configured)

    # ------------------------------------------------------------------
    # LLM 工具路径
    # ------------------------------------------------------------------

    @filter.llm_tool(name="make_meme")
    async def make_meme(
        self, event: AstrMessageEvent, slogan: str, emotion: str = "other"
    ) -> str:
        """生成一张表情包：把简短文案画进模板图左上角的气泡里，并发送。

        文案会被自动适配气泡区域（换行/缩放），绝不会超出气泡范围；超过字数上限会被截断。
        调用后，请在最终回复的末尾单独加一行标记 [meme:FILE_ID]，FILE_ID 用本函数返回值中
        的 file_id 字段；不要复述或解释这个标记。

        Args:
            slogan(string): 画到图上的短文案，要求精炼、有梗，尽量不超过 8 个字。
            emotion(string): 情感分类，用于把成图归档进表情库：happy/sad/angry/surprised/confused/fool/sigh/work/like/meow/baka/other。
        """
        max_chars = int(self._cfg("max_chars"))
        slogan = (slogan or "").strip()
        if not bool(self._cfg("enable_llm_tool")):
            return json.dumps(
                {"ok": False, "reason": "LLM 工具已在插件配置中关闭"},
                ensure_ascii=False,
            )
        if not slogan:
            return json.dumps(
                {"ok": False, "reason": "文案不能为空"}, ensure_ascii=False
            )
        if len(slogan) > max_chars:
            slogan = slogan[:max_chars]
        result = await asyncio.to_thread(self._render_once, slogan, emotion)
        if not result["ok"]:
            return json.dumps(
                {"ok": False, "reason": result["err"]}, ensure_ascii=False
            )
        return json.dumps(
            {"ok": True, "file_id": result["file_id"], "slogan": slogan},
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------
    # 概率拦截路径
    # ------------------------------------------------------------------

    @filter.on_llm_response(priority=0)
    async def on_llm_response_handler(self, event: AstrMessageEvent, response) -> None:
        """LLM 回复草稿生成后，按概率决定是否走「只发图」路径。"""
        text = (getattr(response, "completion_text", "") or "").strip()
        if not text:
            return
        if _MEME_MARKER_RE.search(text):
            return  # 本轮已走 make_meme 工具路径，不再概率拦截
        if not bool(self._cfg("enable_probability")):
            return  # 概率拦截开关已关闭
        if not self._is_group_allowed(event):
            return  # 群聊不在白名单内
        max_text_len = int(self._cfg("max_text_len") or 0)
        if max_text_len > 0 and len(text) > max_text_len:
            return  # 长回复不拦截
        probability = float(self._cfg("probability") or 0)
        if probability <= 0 or random.random() > probability:
            return
        slogan, emotion = await self._summarize(text, event)
        if not slogan:
            return  # 浓缩失败，保持正常文字回复
        result = await asyncio.to_thread(self._render_once, slogan, emotion)
        if not result["ok"]:
            logger.error(f"概率拦截路径渲染失败: {result['err']}")
            return
        event.set_extra("meme_text_replace_image", result["path"])

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        """按群白名单决定是否对当前消息走概率拦截。

        Args:
            event: 消息事件。

        Returns:
            是否允许拦截；白名单为空或非群聊消息时恒为 True。
        """
        whitelist = self._cfg("group_whitelist") or []
        if not whitelist:
            return True
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return True  # 白名单只约束群聊，私聊不受影响
        group_id = event.get_group_id()
        return group_id in [str(item) for item in whitelist]

    async def _summarize(self, text: str, event: AstrMessageEvent) -> tuple[str, str]:
        """用 LLM 把回复草稿浓缩成梗图文案并判断情感。

        Args:
            text: 回复草稿。
            event: 触发本次回复的消息事件（用于取当前会话的对话模型）。

        Returns:
            (浓缩文案, 情感分类)；任一步失败返回 ("", "other")。
        """
        max_chars = int(self._cfg("max_chars"))
        allowlist_str = "/".join(EMOTION_ALLOWLIST)
        system_prompt = (
            "你是表情包文案压缩器。输入是一段聊天回复草稿，请把它浓缩成一个 "
            f"不超过 {max_chars} 个字的梗图文案，并给出情感分类。\n"
            "规则：\n"
            "1. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释。\n"
            f'2. 格式：{{"slogan": "浓缩文案", "emotion": "分类"}}\n'
            f"3. slogan 要有梗、口语化、保留核心情绪，最多 {max_chars} 个字，可补感叹号。\n"
            f"4. emotion 只能取以下之一：{allowlist_str}。\n"
        )
        try:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
            if provider is None:
                return "", "other"
            llm_response = await self.context.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=f"回复草稿：\n{text}\n\n请浓缩并分类。",
                system_prompt=system_prompt,
            )
            slogan, emotion = parse_summarize_output(
                llm_response.completion_text, max_chars, EMOTION_ALLOWLIST
            )
            if not slogan:
                logger.warning("LLM 浓缩输出解析失败，跳过本次拦截")
            return slogan, emotion
        except Exception as err:  # noqa: BLE001 - 浓缩失败则保持正常文字回复
            logger.error(f"LLM 浓缩调用失败: {err}")
            return "", "other"

    # ------------------------------------------------------------------
    # 结果装饰：替换/插入图片
    # ------------------------------------------------------------------

    @filter.on_decorating_result(priority=DECORATE_PRIORITY_LAST)
    async def on_decorating_result_handler(self, event: AstrMessageEvent) -> None:
        """发送前处理消息链：

        - 概率拦截路径命中时，整条消息替换为纯图片（无文字），
          同时天然压掉 meme_manager 本轮插入的任何图片；
        - 显式工具路径命中时，剥离 [meme:...] 标记并把图片追加到文字之后，
          同样丢弃本轮其他图片，避免双梗图。
        """
        result = event.get_result()
        if result is None or not result.chain:
            return
        replace_path = event.get_extra("meme_text_replace_image", "")
        if replace_path and Path(replace_path).is_file():
            result.chain = [Image.fromFileSystem(replace_path)]
            return
        self._append_marked_images(result.chain)

    def _append_marked_images(self, chain: list) -> None:
        """处理 [meme:file_id] 标记：剥离标记、追加图片、丢弃其他图片。

        Args:
            chain: 待处理的消息链（原地修改）。
        """
        new_chain: list = []
        meme_fired = False
        for component in chain:
            if isinstance(component, Plain) and component.text:
                text = component.text
                match = _MEME_MARKER_RE.search(text)
                if match:
                    meme_fired = True
                    stripped = _MEME_MARKER_RE.sub("", text).strip()
                    if stripped:
                        component.text = stripped
                        new_chain.append(component)
                    else:
                        continue
                    self._append_image_for_id(new_chain, match.group(1))
                    continue
            if isinstance(component, Image) and meme_fired:
                continue  # 本轮回合已由本插件出图，丢弃其他图片
            new_chain.append(component)
        chain[:] = new_chain

    @staticmethod
    def _append_image_for_id(chain: list, file_id: str) -> None:
        """把 file_id 对应的本地图片追加进消息链；无效 id 静默忽略。

        Args:
            chain: 消息链。
            file_id: base64 编码的图片绝对路径。
        """
        try:
            path = os.fsdecode(base64.urlsafe_b64decode(file_id.encode()))
        except Exception:  # noqa: BLE001 - 无效 id 静默忽略
            return
        if Path(path).is_file():
            chain.append(Image.fromFileSystem(path))

    # ------------------------------------------------------------------
    # 手动命令（便于无 LLM 时验证全链路）
    # ------------------------------------------------------------------

    @filter.command("meme")
    async def meme_command(self, event: AstrMessageEvent, text: GreedyStr):
        """手动生成表情包：/meme <文案>（空参数显示用法）。"""
        text = (text or "").strip()
        if not text:
            yield event.plain_result("用法：/meme <文案>，例如 /meme 这也行？")
            return
        result = await asyncio.to_thread(self._render_once, text, "other")
        if not result["ok"]:
            yield event.plain_result(f"表情包渲染失败：{result['err']}")
            return
        yield event.image_result(result["path"])

    async def terminate(self):
        """插件卸载时清理。"""
        logger.info("MemeTextPlugin 已卸载")
