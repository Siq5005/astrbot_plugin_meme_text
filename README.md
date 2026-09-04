# astrbot_plugin_meme_text

AstrBot 表情包文字生成插件：给 LLM 提供「把 ≤8 字文案画进固定模板图的气泡里并发送」的能力。

## 功能

- **显式工具路径**：LLM 调用 `make_meme(slogan, emotion)` 工具，自己给文案与情感分类，成图以 `[meme:FILE_ID]` 标记追加到回复文字之后。
- **概率拦截路径**：按配置概率，LLM 的回复草稿会被再一次交给 LLM 浓缩成 ≤8 字文案并判断情感，本轮只发图、不发文字。
- **文案永不越界**：文字自动换行、自适应缩字号，物理上不可能超出模板气泡范围。
- **归档联动**：成图按情感分类复制到 meme_manager 表情库目录（`<data>/plugin_data/meme_manager/packs/<包>/memes/<情感>/`），云同步等交给 meme_manager 自身；目录不可用时只发送不归档，零硬依赖。

## 用法

- LLM 工具：`make_meme`（自动注册，需在会话中开启 LLM 工具/函数调用）。
- 手动命令：`/meme <文案>` —— 无 LLM 也能验证渲染与发送链路。

## 模板与文字框

- 默认模板为 `templates/template.jpg`（左上角含空白思考气泡的图），可在配置里改用 `template_path`。
- 文字框由 `text_box_x/y/w/h`（相对比例 0~1）定义；换模板时按新图气泡调整这四个值即可。

## 配置

见 AstrBot WebUI 的插件配置页，或 `_conf_schema.json`：

| 键 | 默认 | 说明 |
|---|---|---|
| probability | 0.2 | 概率拦截触发概率，0 关闭 |
| max_chars | 8 | 文案最大字数 |
| max_text_len | 30 | 长于该字数的回复不拦截；0=总是 |
| template_path | 空 | 模板图路径 |
| text_box_x/y/w/h | 0.08/0.11/0.44/0.24 | 文字框（相对比例） |
| text_color | #3B176B | 文字颜色 |
| stroke_color/width | 空/0 | 描边 |
| font_path | 空 | 字体覆盖，空则自动探测 |
| meme_manager_memes_dir | 空 | 表情库目录，空则自动探测 |

## 开发

```bash
ruff format .
ruff check .
pytest tests/
```