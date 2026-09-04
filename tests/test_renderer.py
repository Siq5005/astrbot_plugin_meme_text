"""renderer 与 main 纯逻辑的单测。"""

from pathlib import Path

import numpy as np
from astrbot_plugin_meme_text.main import (
    _MEME_MARKER_RE,
    DECORATE_PRIORITY_LAST,
    parse_summarize_output,
    resolve_memes_base_dir,
    resolve_template_path,
)
from astrbot_plugin_meme_text.renderer import (
    _tokenize,
    fit_font_size,
    render_meme,
    resolve_font_path,
    wrap_text,
)
from PIL import Image

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = PLUGIN_DIR / "templates" / "template.jpg"
DEFAULT_BOX = {"x": 0.08, "y": 0.11, "w": 0.44, "h": 0.24}


def test_resolve_font_path_returns_existing_file() -> None:
    """字体探测应返回一个真实存在的字体文件。"""
    font = resolve_font_path("")
    assert Path(font).is_file()


def test_resolve_font_path_prefers_configured() -> None:
    """配置的字体路径优先于候选列表。"""
    font = resolve_font_path("")
    same = resolve_font_path(font)
    assert same == str(Path(font).resolve())


def test_tokenize_cjk_and_latin() -> None:
    """CJK 逐字、拉丁词整体成单元、空白剔除。"""
    assert _tokenize("这也行？") == ["这", "也", "行", "？"]
    assert _tokenize("hello 世界") == ["hello", "世", "界"]


def test_wrap_text_single_line_when_fits() -> None:
    """宽度足够时保持单行。"""
    font_path = resolve_font_path("")
    from PIL import ImageFont

    font = ImageFont.truetype(font_path, 60)
    lines = wrap_text("这也行？", font, max_width=1000)
    assert lines == ["这也行？"]


def test_fit_font_size_truncates_long_text() -> None:
    """极小文字框下长文案应被截断且行数受限。"""
    font_path = resolve_font_path("")
    text = "这是一段非常非常长的文案" * 8  # 80 字，绝无可能装进两行
    size, lines, truncated, warn = fit_font_size(text, font_path, 300, 40, 2)
    assert truncated is True
    assert warn is not None
    assert len(lines) <= 2
    assert size > 0


def _render_box_px(box: dict) -> tuple[int, int, int, int]:
    """把相对文字框换算成像素边界（基于模板尺寸 1280x1280）。"""
    with Image.open(TEMPLATE) as img:
        width, height = img.size
    bx = round(box["x"] * width)
    by = round(box["y"] * height)
    bw = round(box["w"] * width)
    bh = round(box["h"] * height)
    return bx, by, bx + bw, by + bh


def _text_pixel_bbox(left: str, right: str) -> tuple[int, int, int, int]:
    """两次渲染（不同文字颜色）逐像素取差，差集即文字像素。

    Returns:
        文字像素的外接矩形 (x0, y0, x1, y1)；无文字时返回 (0, 0, 0, 0)。
    """
    render_meme(
        str(TEMPLATE),
        "这也行",
        DEFAULT_BOX,
        resolve_font_path(""),
        color="#FF00FF",
        out_path=left,
    )
    render_meme(
        str(TEMPLATE),
        "这也行",
        DEFAULT_BOX,
        resolve_font_path(""),
        color="#00FF00",
        out_path=right,
    )
    a = np.asarray(Image.open(left).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(right).convert("RGB"), dtype=np.int16)
    diff = np.any(np.abs(a - b) > 10, axis=2)
    ys, xs = np.where(diff)
    if ys.size == 0:
        return 0, 0, 0, 0
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def test_render_text_stays_inside_box(tmp_path: Path) -> None:
    """渲染出的文字像素必须全部落在文字框内（不越界保证）。"""
    left = str(tmp_path / "l.png")
    right = str(tmp_path / "r.png")
    box = DEFAULT_BOX
    bx0, by0, bx1, by1 = _render_box_px(box)
    x0, y0, x1, y1 = _text_pixel_bbox(left, right)
    assert x1 > x0 and y1 > y0, "应渲染出可见文字"
    assert x0 >= bx0 and y0 >= by0 and x1 <= bx1 and y1 <= by1, (
        f"文字越界: text=({x0},{y0})-({x1},{y1}) box=({bx0},{by0})-({bx1},{by1})"
    )
    with Image.open(left) as img:
        assert img.size == (1280, 1280)
        assert img.format == "PNG"


def test_render_long_text_still_within_box(tmp_path: Path) -> None:
    """超长文案被截断/缩放后仍然不越界。"""
    long_text = "这是一段非常非常长的文案完全超出气泡范围肯定装不下怎么办"
    left = str(tmp_path / "l.png")
    right = str(tmp_path / "r.png")
    render_meme(
        str(TEMPLATE),
        long_text,
        DEFAULT_BOX,
        resolve_font_path(""),
        color="#FF00FF",
        out_path=left,
    )
    render_meme(
        str(TEMPLATE),
        long_text,
        DEFAULT_BOX,
        resolve_font_path(""),
        color="#00FF00",
        out_path=right,
    )
    a = np.asarray(Image.open(left).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(right).convert("RGB"), dtype=np.int16)
    diff = np.any(np.abs(a - b) > 10, axis=2)
    ys, xs = np.where(diff)
    assert xs.size > 0
    bx0, by0, bx1, by1 = _render_box_px(DEFAULT_BOX)
    assert int(xs.min()) >= bx0 and int(ys.min()) >= by0
    assert int(xs.max()) <= bx1 and int(ys.max()) <= by1


def test_parse_summarize_output_variants() -> None:
    """浓缩输出解析：JSON 本体、代码块、非法情感、无效文本。"""
    allowlist = ("happy", "other")
    assert parse_summarize_output(
        '{"slogan": "这也行？", "emotion": "happy"}', 8, allowlist
    ) == ("这也行？", "happy")
    assert parse_summarize_output(
        '```json\n{"slogan": "绝了！", "emotion": "happy"}\n```', 8, allowlist
    ) == ("绝了！", "happy")
    # 情感白名单外一律 other
    assert parse_summarize_output(
        '{"slogan": "离谱", "emotion": "wtf"}', 8, allowlist
    ) == ("离谱", "other")
    # 超长文案截断
    assert parse_summarize_output(
        '{"slogan": "一二三四五六七八九十", "emotion": "happy"}', 8, allowlist
    ) == ("一二三四五六七八", "happy")
    # 完全无效
    assert parse_summarize_output("完全不是json", 8, allowlist) == ("", "other")


def test_meme_marker_regex() -> None:
    """[meme:file_id] 标记匹配与剥离。"""
    text = "就这？[meme:QUJD]"
    match = _MEME_MARKER_RE.search(text)
    assert match and match.group(1) == "QUJD"
    assert _MEME_MARKER_RE.sub("", text).strip() == "就这？"


def test_decorate_priority_runs_last() -> None:
    """装饰钩子优先级应远低于默认 0，保证最后执行覆盖其它插件。"""
    assert DECORATE_PRIORITY_LAST < 0


def test_resolve_template_path(tmp_path) -> None:
    """WebUI file 配置的相对路径应解析到插件数据目录，且后上传优先。"""
    root = tmp_path / "plugin_data"
    old = root / "files" / "template_path" / "old.png"
    new = root / "files" / "template_path" / "new.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    assert resolve_template_path(
        ["files/template_path/old.png"], plugin_data_root=str(root)
    ) == str(old)
    # 后上传的优先
    assert resolve_template_path(
        ["files/template_path/old.png", "files/template_path/new.png"],
        plugin_data_root=str(root),
    ) == str(new)
    # 绝对路径兼容
    assert resolve_template_path([str(old)], plugin_data_root=str(root)) == str(old)
    # 无可用配置 → None（调用方回退内置模板）
    assert resolve_template_path([]) is None
    assert resolve_template_path("") is None
    assert (
        resolve_template_path(
            ["files/template_path/missing.png"], plugin_data_root=str(root)
        )
        is None
    )


def test_resolve_memes_base_dir_flat_layout(tmp_path) -> None:
    """meme_manager 3.x 扁平布局 plugin_data/meme_manager/memes 应能被探测。"""
    root = tmp_path / "data"
    flat = root / "plugin_data" / "meme_manager" / "memes" / "happy"
    flat.mkdir(parents=True)
    assert resolve_memes_base_dir("", data_root=root) == flat.parent


def test_resolve_memes_base_dir_packs_preferred(tmp_path) -> None:
    """meme_manager 4.x packs 布局优先于扁平布局。"""
    root = tmp_path / "data"
    packs = (
        root / "plugin_data" / "meme_manager" / "packs" / "builtin-default" / "memes"
    )
    flat = root / "plugin_data" / "meme_manager" / "memes"
    packs.mkdir(parents=True)
    flat.mkdir(parents=True)
    assert resolve_memes_base_dir("", data_root=root) == packs


def test_resolve_memes_base_dir_config_and_missing(tmp_path) -> None:
    """配置值优先；全部缺失返回 None。"""
    root = tmp_path / "data"
    configured = tmp_path / "custom_memes"
    configured.mkdir()
    assert resolve_memes_base_dir(str(configured), data_root=root) == configured
    assert resolve_memes_base_dir("", data_root=root) is None


def test_command_param_binding_uses_greedy_string() -> None:
    """/meme 命令参数按框架规则用 GreedyStr 绑定。

    回归：旧写法 (self, event, *args) 会让框架把无注解的 VAR_POSITIONAL
    当成类型转换，从而抛出 `_empty() takes no arguments`。
    """
    from astrbot.core.star.filter.command import CommandFilter, GreedyStr

    cmd = CommandFilter("meme")
    # 空参数 → 空字符串（命令体内显示用法）
    assert cmd.validate_and_convert_params([], {"text": GreedyStr}) == {"text": ""}
    # 多段文本 → 吞掉剩余全部参数并合并
    assert cmd.validate_and_convert_params(
        ["这也行？", "好不好"], {"text": GreedyStr}
    ) == {"text": "这也行？ 好不好"}


def test_render_downscale_to_max_size(tmp_path) -> None:
    """max_size 生效时输出最长边不超过限制且保持宽高比。"""
    out = str(tmp_path / "scaled.png")
    render_meme(
        str(TEMPLATE),
        "这也行？",
        DEFAULT_BOX,
        resolve_font_path(""),
        out_path=out,
        max_size=512,
    )
    with Image.open(out) as img:
        assert max(img.size) <= 512, img.size
    # 原图 1280x1280 正方形，缩小后仍为正方形
    assert img.size[0] == img.size[1]
