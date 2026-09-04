# astrbot_plugin_meme_text

AstrBot 表情包文字生成插件：给 LLM 提供「把 ≤8 字文案画进固定模板图的气泡里并发送」的能力。

## 功能

- **LLM 工具路径**：LLM 调用 `make_meme(slogan, emotion)` 工具，自己给文案与情感分类，成图以 `[meme:FILE_ID]` 标记追加到回复文字之后。
- **概率拦截路径**：按配置概率，LLM 的回复草稿会再一次交给 LLM 浓缩成 ≤8 字文案并判断情感，本轮只发图、不发文字；可只对指定群生效。
- **文案永不越界**：文字自动换行、自适应缩字号，物理上不可能超出模板气泡范围。
- **醒目默认**：黑色粗体文字（同色细描边假粗体，不依赖系统粗体字体）。
- **归档联动**：成图按情感分类复制到 meme_manager 表情库目录（`<data>/plugin_data/meme_manager/packs/<包>/memes/<情感>/`），云同步等交给 meme_manager 自身；目录不可用时只发送不归档，零硬依赖。

## 用法

- LLM 工具：`make_meme`（可在插件配置里关闭）。
- 手动命令：`/meme <文案>` —— 无 LLM 也能验证渲染与发送链路。
- 概率拦截：`enable_probability` 开启后，命中概率 + 短回复 + 群白名单（如配置）即只发图。

## 模板与文字框

- 默认模板为 `templates/template.jpg`（左上角空白思考气泡）。
- **在 WebUI 插件配置页的「模板图片」里直接上传即可更换**（支持 jpg/png/webp，存放在插件数据目录）；也可手动填路径。
- 换模板后按新图气泡调整 `text_box_x/y/w/h`（相对比例 0~1）即可。

## 配置

见 AstrBot WebUI 的插件配置页，或 `_conf_schema.json`：

| 键 | 默认 | 说明 |
|---|---|---|
| enable_llm_tool | true | LLM 工具路径开关 |
| enable_probability | true | 概率拦截路径开关 |
| group_whitelist | [] | 只拦短回复的群聊白名单（群号）；空=所有群 |
| probability | 0.2 | 概率拦截触发概率，0 关闭 |
| max_chars | 8 | 文案最大字数 |
| max_text_len | 30 | 长于该字数的回复不拦截；0=总是 |
| template_path | [] | WebUI 上传的模板图片（file 类型） |
| text_box_x/y/w/h | 0.08/0.11/0.44/0.24 | 文字框（相对比例） |
| bold | true | 文字加粗（假粗体） |
| text_color | #000000 | 文字颜色 |
| stroke_color/width | 空/0 | 描边 |
| font_path | 空 | 字体覆盖，空则自动探测（粗体优先） |
| meme_manager_memes_dir | 空 | 表情库目录，空则自动探测 |

## 开发

```bash
ruff format .
ruff check .
PYTHONPATH=/path/to/AstrBot /path/to/AstrBot/.venv/bin/python -m pytest tests/
```