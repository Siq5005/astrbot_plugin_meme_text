"""表情包渲染引擎：在模板图上把文案画进指定的文字框，保证文字永不越界。

核心思路是「先量后画」：不断缩小字号、按字贪心换行，直到文案在
文字框内装下；若字号已小到阈值仍装不下，才截断加省略号。因此无论
文案多长，最终像素一定落在文字框范围内。
"""

from __future__ import annotations

import tempfile
import unicodedata
from pathlib import Path
from typing import TypedDict

from PIL import Image, ImageDraw, ImageFont

# 常见中文字体候选，按平台顺序探测，粗体/中黑字体优先；配置 font_path 可覆盖
FONT_CANDIDATES: tuple[str, ...] = (
    # macOS（STHeiti Medium 自带中黑字重；Hiragino Sans GB 偏细放最后）
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Windows
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)

MIN_FONT_RATIO = 0.035  # 字号相对图片高度的下限，低于此值直接截断
LINE_SPACING_RATIO = 1.18  # 行高 = 字号 * 该系数
BOLD_STROKE = 2  # 假粗体：无描边配置时用同色描边实现，像素宽度固定，保证框内排版可预期


class TextBox(TypedDict):
    """文字框，坐标为相对比例（0~1，乘图片宽/高得到像素）。"""

    x: float
    y: float
    w: float
    h: float


class RenderResult(TypedDict):
    """渲染结果。"""

    path: str
    font_size: int
    lines: list[str]
    truncated: bool
    warn: str | None


def resolve_font_path(configured: str = "") -> str:
    """返回可用的中文字体路径。

    Args:
        configured: 配置里的字体路径，非空且存在时优先使用。

    Returns:
        字体文件绝对路径。

    Raises:
        FileNotFoundError: 配置路径无效且所有候选字体都不存在。
    """
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError(
        f"找不到中文字体（已尝试配置 {configured or '空'} 与候选列表 "
        f"{FONT_CANDIDATES}）。请在插件配置里填写 font_path。"
    )


def _tokenize(text: str) -> list[str]:
    """把文本拆成「绘制单元」：CJK 逐字、拉丁词整体一个单元。

    Args:
        text: 待拆分的文本。

    Returns:
        单元列表，不含空白。
    """
    tokens: list[str] = []
    word = ""
    for char in text.strip():
        width = unicodedata.east_asian_width(char)
        is_cjk = width in ("W", "F") or (char > "\u2e80" and not char.isascii())
        if is_cjk or char.isspace():
            if word:
                tokens.append(word)
                word = ""
            if not char.isspace():
                tokens.append(char)
        else:
            word += char
    if word:
        tokens.append(word)
    return tokens


def wrap_text(
    text: str, font: ImageFont.FreeTypeFont, max_width: int, stroke: int = 0
) -> list[str]:
    """按绘制单元贪心换行。

    Args:
        text: 待换行文本。
        font: 已加载字体。
        max_width: 单行最大像素宽度。
        stroke: 描边宽度，画布占用按 2*stroke 计入。

    Returns:
        换行后的行列表（至少一行）。
    """
    tokens = _tokenize(text)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    pad = 2 * stroke
    lines: list[str] = []
    line = ""
    for token in tokens:
        candidate = token if not line else f"{line}{token}"
        width = draw.textlength(candidate, font=font) + pad
        if width <= max_width or not line:
            line = candidate
        else:
            lines.append(line)
            line = token
    if line:
        lines.append(line)
    return lines or [text[:1] or "…"]


def _text_height(font: ImageFont.FreeTypeFont) -> int:
    """单行文本的标准高度（含行距）。"""
    bbox = font.getbbox("中")
    return round((bbox[3] - bbox[1]) * LINE_SPACING_RATIO)


def fit_font_size(
    text: str,
    font_path: str,
    box_w: int,
    box_h: int,
    max_lines: int,
    stroke: int = 0,
) -> tuple[int, list[str], bool, str | None]:
    """计算能让文本完整装进文字框的字号。

    Args:
        text: 待绘制文本。
        font_path: 字体文件路径。
        box_w: 文字框像素宽度。
        box_h: 文字框像素高度。
        max_lines: 允许的最大行数。
        stroke: 描边宽度。

    Returns:
        (字号, 最终行, 是否被截断, 警告信息)。
    """
    start_size = max(8, int(box_h * 0.85 / max_lines))
    min_size = max(8, int(box_h * MIN_FONT_RATIO))
    warn: str | None = None
    for size in range(start_size, min_size - 1, -1):
        font = ImageFont.truetype(font_path, size)
        lines = wrap_text(text, font, box_w, stroke)
        total_height = _text_height(font) * len(lines)
        if len(lines) <= max_lines and total_height <= box_h:
            return size, lines, False, None
    # 最小字号仍装不下：截断到可容纳的行数
    font = ImageFont.truetype(font_path, min_size)
    warn = "文案过长，已截断"
    lines = wrap_text(text, font, box_w, stroke)[:max_lines]
    truncated = True
    if lines:
        last = lines[-1]
        if len(last) > 1:
            lines[-1] = last[:-1] + "…"
    return min_size, lines, truncated, warn


def render_meme(
    template_path: str,
    text: str,
    box: TextBox,
    font_path: str,
    color: str = "#000000",
    stroke_color: str = "",
    stroke_width: int = 0,
    max_lines: int = 3,
    out_path: str | None = None,
    bold: bool = True,
    max_size: int = 0,
) -> RenderResult:
    """把文案渲染到模板图的文字框内，输出 PNG。

    Args:
        template_path: 模板图路径。
        text: 文案；为空时画省略号。
        box: 文字框（相对比例坐标）。
        font_path: 字体文件路径。
        color: 文字颜色。
        stroke_color: 描边颜色，空字符串表示不描边。
        stroke_width: 描边宽度（像素）；仅当 stroke_color 非空时生效。
        max_lines: 最大行数。
        out_path: 输出路径；为 None 时自动生成。
        bold: 是否加粗。无描边配置时用同色细描边实现假粗体，
            使其更醒目且不依赖系统粗体字体。
        max_size: 输出图片最长边像素上限，超出则等比缩小（文字画完后再
            缩放，气泡与文字比例不变）；0 表示不缩小。

    Returns:
        RenderResult: 输出路径与渲染信息。

    Raises:
        FileNotFoundError: 模板图不存在。
    """
    source = Path(template_path)
    if not source.is_file():
        raise FileNotFoundError(f"模板图不存在: {template_path}")

    with Image.open(source) as opened:
        image = opened.convert("RGB")
    width, height = image.size

    bx = round(box["x"] * width)
    by = round(box["y"] * height)
    bw = round(box["w"] * width)
    bh = round(box["h"] * height)
    # 兜底：把越界的框裁剪进图片内
    bx = max(0, min(bx, width - 1))
    by = max(0, min(by, height - 1))
    bw = max(1, min(bw, width - bx))
    bh = max(1, min(bh, height - by))

    content = (text or "").strip() or "…"
    # 有效描边：显式配置优先；否则加粗时用同色细描边实现假粗体
    has_stroke = bool(stroke_color)
    stroke = max(0, int(stroke_width)) if has_stroke else (BOLD_STROKE if bold else 0)
    font_size, lines, truncated, warn = fit_font_size(
        content, font_path, bw, bh, max_lines, stroke
    )
    font = ImageFont.truetype(font_path, font_size)

    draw = ImageDraw.Draw(image)
    line_height = _text_height(font) + 2 * stroke
    total_height = line_height * len(lines)
    cursor_y = by + max(0, (bh - total_height) // 2)
    for line in lines:
        line_width = draw.textlength(line, font=font) + 2 * stroke
        cursor_x = bx + max(0, (bw - line_width) // 2)
        draw.text(
            (cursor_x + stroke, cursor_y + stroke),
            line,
            font=font,
            fill=color,
            stroke_width=stroke,
            stroke_fill=stroke_color if has_stroke else (color if bold else None),
        )
        cursor_y += line_height

    if max_size > 0 and max(width, height) > max_size:
        # 文字绘制完成后再整体等比缩小，气泡与文字相对位置不变
        scale = max_size / max(width, height)
        image = image.resize(
            (round(width * scale), round(height * scale)), Image.Resampling.LANCZOS
        )

    if out_path is None:
        out_path = str(Path(tempfile.gettempdir()) / "meme_text_render.png")
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG")

    return RenderResult(
        path=str(output),
        font_size=font_size,
        lines=lines,
        truncated=truncated,
        warn=warn,
    )
