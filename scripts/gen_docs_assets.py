#!/usr/bin/env python3
"""Generate docs/assets/architecture.svg and docs/assets/demo.gif."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REG
    try:
        return ImageFont.truetype(path, size=size, index=0)
    except OSError:
        return ImageFont.load_default()


def write_architecture_svg() -> Path:
    w, h = 960, 620
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="Agent-Driven Requirements architecture">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#1e293b"/>
    </linearGradient>
    <filter id="shadow" x="-5%" y="-5%" width="110%" height="120%">
      <feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#000" flood-opacity="0.35"/>
    </filter>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8"/>
    </marker>
  </defs>
  <rect width="{w}" height="{h}" rx="16" fill="url(#bg)"/>
  <text x="32" y="40" fill="#e2e8f0" font-family="Noto Sans CJK SC, Noto Sans, sans-serif" font-size="20" font-weight="700">Architecture · 架构</text>
  <text x="32" y="64" fill="#94a3b8" font-family="Noto Sans CJK SC, Noto Sans, sans-serif" font-size="12">Feishu / Lark chat → long connection → intent → record / query → Bitable · SQLite</text>

  <g filter="url(#shadow)">
    <rect x="40" y="100" width="200" height="72" rx="12" fill="#1e3a5f" stroke="#38bdf8" stroke-width="1.5"/>
    <text x="140" y="130" text-anchor="middle" fill="#e0f2fe" font-family="Noto Sans CJK SC, sans-serif" font-size="14" font-weight="700">Feishu / Lark Chat</text>
    <text x="140" y="152" text-anchor="middle" fill="#7dd3fc" font-family="Noto Sans CJK SC, sans-serif" font-size="11">群 @ · 单聊 p2p</text>
  </g>

  <g filter="url(#shadow)">
    <rect x="300" y="100" width="200" height="72" rx="12" fill="#312e81" stroke="#a5b4fc" stroke-width="1.5"/>
    <text x="400" y="130" text-anchor="middle" fill="#e0e7ff" font-family="Noto Sans CJK SC, sans-serif" font-size="14" font-weight="700">WebSocket</text>
    <text x="400" y="152" text-anchor="middle" fill="#c7d2fe" font-family="Noto Sans CJK SC, sans-serif" font-size="11">长连接 · im.message.receive</text>
  </g>

  <g filter="url(#shadow)">
    <rect x="560" y="100" width="200" height="72" rx="12" fill="#134e4a" stroke="#2dd4bf" stroke-width="1.5"/>
    <text x="660" y="130" text-anchor="middle" fill="#ccfbf1" font-family="Noto Sans CJK SC, sans-serif" font-size="14" font-weight="700">Intent · 意图</text>
    <text x="660" y="152" text-anchor="middle" fill="#99f6e4" font-family="Noto Sans CJK SC, sans-serif" font-size="11">record | query · LLM</text>
  </g>

  <line x1="240" y1="136" x2="300" y2="136" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>
  <line x1="500" y1="136" x2="560" y2="136" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>

  <line x1="660" y1="172" x2="660" y2="210" stroke="#94a3b8" stroke-width="2"/>
  <line x1="250" y1="210" x2="850" y2="210" stroke="#94a3b8" stroke-width="2"/>
  <line x1="300" y1="210" x2="300" y2="230" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>
  <line x1="720" y1="210" x2="720" y2="230" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>

  <g filter="url(#shadow)">
    <rect x="40" y="230" width="520" height="250" rx="14" fill="#0f172a" stroke="#475569" stroke-width="1.5"/>
    <rect x="40" y="230" width="520" height="36" rx="14" fill="#1e293b"/>
    <rect x="40" y="250" width="520" height="16" fill="#1e293b"/>
    <text x="56" y="254" fill="#fbbf24" font-family="Noto Sans CJK SC, sans-serif" font-size="13" font-weight="700">Record path · 记录路径</text>

    <rect x="56" y="280" width="140" height="56" rx="10" fill="#1e293b" stroke="#64748b"/>
    <text x="126" y="304" text-anchor="middle" fill="#e2e8f0" font-size="12" font-family="Noto Sans CJK SC, sans-serif" font-weight="700">Context</text>
    <text x="126" y="322" text-anchor="middle" fill="#94a3b8" font-size="10" font-family="Noto Sans CJK SC, sans-serif">上下文收集</text>

    <line x1="196" y1="308" x2="220" y2="308" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>

    <rect x="220" y="280" width="140" height="56" rx="10" fill="#1e293b" stroke="#64748b"/>
    <text x="290" y="304" text-anchor="middle" fill="#e2e8f0" font-size="12" font-family="Noto Sans CJK SC, sans-serif" font-weight="700">LLM Extract</text>
    <text x="290" y="322" text-anchor="middle" fill="#94a3b8" font-size="10" font-family="Noto Sans CJK SC, sans-serif">字段抽取</text>

    <line x1="360" y1="308" x2="384" y2="308" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>

    <rect x="384" y="280" width="140" height="56" rx="10" fill="#1e293b" stroke="#64748b"/>
    <text x="454" y="304" text-anchor="middle" fill="#e2e8f0" font-size="12" font-family="Noto Sans CJK SC, sans-serif" font-weight="700">Validate</text>
    <text x="454" y="322" text-anchor="middle" fill="#94a3b8" font-size="10" font-family="Noto Sans CJK SC, sans-serif">校验 / 归一化</text>

    <line x1="454" y1="336" x2="454" y2="360" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>

    <rect x="220" y="360" width="140" height="56" rx="10" fill="#1e293b" stroke="#fbbf24"/>
    <text x="290" y="384" text-anchor="middle" fill="#fde68a" font-size="12" font-family="Noto Sans CJK SC, sans-serif" font-weight="700">Confirm?</text>
    <text x="290" y="402" text-anchor="middle" fill="#fcd34d" font-size="10" font-family="Noto Sans CJK SC, sans-serif">可选写前确认</text>

    <line x1="360" y1="388" x2="384" y2="388" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>

    <rect x="384" y="360" width="140" height="56" rx="10" fill="#14532d" stroke="#4ade80"/>
    <text x="454" y="384" text-anchor="middle" fill="#bbf7d0" font-size="12" font-family="Noto Sans CJK SC, sans-serif" font-weight="700">Bitable Write</text>
    <text x="454" y="402" text-anchor="middle" fill="#86efac" font-size="10" font-family="Noto Sans CJK SC, sans-serif">写入多维表格</text>

    <line x1="290" y1="336" x2="290" y2="360" stroke="#94a3b8" stroke-width="1.5" stroke-dasharray="4 3"/>
  </g>

  <g filter="url(#shadow)">
    <rect x="580" y="230" width="340" height="250" rx="14" fill="#0f172a" stroke="#475569" stroke-width="1.5"/>
    <rect x="580" y="230" width="340" height="36" rx="14" fill="#1e293b"/>
    <rect x="580" y="250" width="340" height="16" fill="#1e293b"/>
    <text x="596" y="254" fill="#38bdf8" font-family="Noto Sans CJK SC, sans-serif" font-size="13" font-weight="700">Query path · 查询路径</text>

    <rect x="610" y="290" width="280" height="48" rx="10" fill="#1e293b" stroke="#64748b"/>
    <text x="750" y="312" text-anchor="middle" fill="#e2e8f0" font-size="12" font-family="Noto Sans CJK SC, sans-serif" font-weight="700">Bitable Search</text>
    <text x="750" y="328" text-anchor="middle" fill="#94a3b8" font-size="10" font-family="Noto Sans CJK SC, sans-serif">关键字 contains 检索</text>

    <line x1="750" y1="338" x2="750" y2="358" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>

    <rect x="610" y="358" width="280" height="48" rx="10" fill="#1e293b" stroke="#64748b"/>
    <text x="750" y="380" text-anchor="middle" fill="#e2e8f0" font-size="12" font-family="Noto Sans CJK SC, sans-serif" font-weight="700">LLM Rerank</text>
    <text x="750" y="396" text-anchor="middle" fill="#94a3b8" font-size="10" font-family="Noto Sans CJK SC, sans-serif">重排 · Top-N 汇总</text>

    <line x1="750" y1="406" x2="750" y2="426" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>

    <rect x="610" y="426" width="280" height="36" rx="10" fill="#1e3a5f" stroke="#38bdf8"/>
    <text x="750" y="449" text-anchor="middle" fill="#e0f2fe" font-size="12" font-family="Noto Sans CJK SC, sans-serif" font-weight="700">Reply · 回复进展</text>
  </g>

  <g filter="url(#shadow)">
    <rect x="40" y="510" width="880" height="72" rx="12" fill="#1e293b" stroke="#818cf8" stroke-width="1.5"/>
    <text x="480" y="542" text-anchor="middle" fill="#e0e7ff" font-family="Noto Sans CJK SC, sans-serif" font-size="14" font-weight="700">SQLite · session / dedup / audit / guided_chats</text>
    <text x="480" y="564" text-anchor="middle" fill="#a5b4fc" font-family="Noto Sans CJK SC, sans-serif" font-size="11">会话状态 · 幂等去重 · 审计 · 首次引导</text>
  </g>

  <line x1="300" y1="480" x2="300" y2="510" stroke="#818cf8" stroke-width="1.5" stroke-dasharray="4 3" marker-end="url(#arrow)"/>
  <line x1="720" y1="480" x2="720" y2="510" stroke="#818cf8" stroke-width="1.5" stroke-dasharray="4 3" marker-end="url(#arrow)"/>
</svg>
'''
    out = ASSETS / "architecture.svg"
    out.write_text(svg, encoding="utf-8")
    return out


W, H = 920, 640
BG = (15, 23, 42)
PANEL = (30, 41, 59)
USER_BUBBLE = (37, 99, 235)
BOT_BUBBLE = (51, 65, 85)
CARD_BG = (30, 41, 59)
CARD_BORDER = (251, 191, 36)
TEXT = (226, 232, 240)
MUTED = (148, 163, 184)
GREEN = (74, 222, 128)
ACCENT = (56, 189, 248)
SUCCESS = (134, 239, 172)


def rounded_rect(draw, xy, r, fill, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=r, fill=fill, outline=outline, width=width)


def draw_avatar(draw, x, y, color, letter):
    draw.ellipse((x, y, x + 28, y + 28), fill=color)
    draw.text((x + 8, y + 4), letter, font=font(14, True), fill=(255, 255, 255))


def draw_chrome(img: Image.Image, title: str = "产品协作群") -> ImageDraw.ImageDraw:
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, W, H), fill=BG)
    rounded_rect(draw, (16, 16, W - 16, 64), 12, PANEL)
    draw.text((36, 30), "飞书 · " + title, font=font(18, True), fill=TEXT)
    # online pill
    rounded_rect(draw, (W - 168, 28, W - 36, 52), 10, (6, 78, 59))
    draw.ellipse((W - 156, 35, W - 144, 47), fill=GREEN)
    draw.text((W - 138, 32), "RecordBot", font=font(12), fill=SUCCESS)
    rounded_rect(draw, (16, 76, W - 16, H - 16), 12, (17, 24, 39))
    return draw


def bubble_user(draw, y: int, text: str, name: str = "Alex") -> int:
    f = font(15)
    pad_x, pad_y = 14, 10
    bbox = draw.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bw, bh = tw + pad_x * 2, th + pad_y * 2
    x1 = W - 56
    x0 = x1 - bw
    draw_avatar(draw, W - 52, y + 10, (59, 130, 246), "A")
    draw.text((x1 - draw.textbbox((0, 0), name, font=font(11))[2], y), name, font=font(11), fill=MUTED)
    y += 18
    rounded_rect(draw, (x0, y, x1, y + bh), 14, USER_BUBBLE)
    draw.text((x0 + pad_x, y + pad_y), text, font=f, fill=(255, 255, 255))
    return y + bh + 18


def bubble_bot(draw, y: int, lines: list[str], name: str = "RecordBot") -> int:
    f = font(15)
    pad_x, pad_y = 14, 10
    max_w = 0
    total_h = 0
    line_sizes = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        line_sizes.append((tw, th))
        max_w = max(max_w, tw)
        total_h += th + 6
    total_h = max(total_h - 6, 0) + pad_y * 2
    bw = min(max_w + pad_x * 2, W - 120)
    x0 = 56
    draw_avatar(draw, 24, y + 10, (99, 102, 241), "R")
    draw.text((x0, y), name, font=font(11), fill=MUTED)
    y += 18
    rounded_rect(draw, (x0, y, x0 + bw, y + total_h), 14, BOT_BUBBLE)
    cy = y + pad_y
    for i, line in enumerate(lines):
        color = SUCCESS if line.startswith("✓") else (ACCENT if line.startswith("→") else TEXT)
        draw.text((x0 + pad_x, cy), line, font=f, fill=color)
        cy += line_sizes[i][1] + 6
    return y + total_h + 18


def bubble_card(draw, y: int, title: str, fields: list[tuple[str, str]], footer: str) -> int:
    x0, x1 = 56, 540
    h = 24 + 22 + len(fields) * 28 + 40 + 12
    draw_avatar(draw, 24, y + 10, (99, 102, 241), "R")
    draw.text((x0, y), "RecordBot", font=font(11), fill=MUTED)
    y += 18
    rounded_rect(draw, (x0, y, x1, y + h), 14, CARD_BG, outline=CARD_BORDER, width=2)
    draw.rectangle((x0 + 2, y + 2, x1 - 2, y + 38), fill=(55, 48, 20))
    draw.text((x0 + 16, y + 10), title, font=font(14, True), fill=(253, 230, 138))
    cy = y + 50
    for k, v in fields:
        draw.text((x0 + 16, cy), f"· {k}：{v}", font=font(14), fill=TEXT)
        cy += 28
    draw.text((x0 + 16, cy + 2), footer, font=font(13), fill=ACCENT)
    return y + h + 18


def bubble_typing(draw, y: int) -> int:
    draw_avatar(draw, 24, y + 10, (99, 102, 241), "R")
    draw.text((56, y), "RecordBot", font=font(11), fill=MUTED)
    y += 18
    rounded_rect(draw, (56, y, 176, y + 40), 14, BOT_BUBBLE)
    for i, dx in enumerate((18, 40, 62)):
        # pulsing-ish sizes
        r = 5 + (i % 2)
        draw.ellipse((56 + dx, y + 16 - r // 2, 56 + dx + r * 2, y + 16 + r * 2 - r // 2), fill=MUTED)
    return y + 58


def make_frames() -> list[tuple[Image.Image, int]]:
    frames: list[tuple[Image.Image, int]] = []

    img = Image.new("RGB", (W, H))
    draw = draw_chrome(img)
    bubble_user(draw, 100, "@RecordBot 订单导出太慢了，P1，周五前要")
    frames.append((img, 1400))

    img = Image.new("RGB", (W, H))
    draw = draw_chrome(img)
    y = bubble_user(draw, 100, "@RecordBot 订单导出太慢了，P1，周五前要")
    bubble_typing(draw, y)
    frames.append((img, 900))

    img = Image.new("RGB", (W, H))
    draw = draw_chrome(img)
    y = bubble_user(draw, 100, "@RecordBot 订单导出太慢了，P1，周五前要")
    bubble_bot(draw, y, ["收到，正在整理…"])
    frames.append((img, 1100))

    img = Image.new("RGB", (W, H))
    draw = draw_chrome(img)
    y = bubble_user(draw, 92, "@RecordBot 订单导出太慢了，P1，周五前要")
    bubble_card(
        draw,
        y,
        "预览 → 缺陷登记",
        [
            ("标题", "订单导出过慢"),
            ("严重程度", "P1"),
            ("截止日期", "本周五"),
        ],
        "回复「好」写入，或直接改某一项",
    )
    frames.append((img, 2200))

    img = Image.new("RGB", (W, H))
    draw = draw_chrome(img)
    y = bubble_user(draw, 92, "@RecordBot 订单导出太慢了，P1，周五前要")
    y = bubble_card(
        draw,
        y,
        "预览 → 缺陷登记",
        [
            ("标题", "订单导出过慢"),
            ("严重程度", "P1"),
            ("截止日期", "本周五"),
        ],
        "回复「好」写入，或直接改某一项",
    )
    # may overflow — shrink by omitting long first bubble on confirm frame
    # Keep as-is if fits; otherwise start lower content only
    if y < H - 80:
        bubble_user(draw, y, "好")
    else:
        # rebuild compact
        img = Image.new("RGB", (W, H))
        draw = draw_chrome(img)
        y = 92
        y = bubble_card(
            draw,
            y,
            "预览 → 缺陷登记",
            [
                ("标题", "订单导出过慢"),
                ("严重程度", "P1"),
                ("截止日期", "本周五"),
            ],
            "回复「好」写入，或直接改某一项",
        )
        bubble_user(draw, y, "好")
    frames.append((img, 1200))

    img = Image.new("RGB", (W, H))
    draw = draw_chrome(img)
    y = 110
    bubble_bot(
        draw,
        y,
        [
            "✓ 已记录",
            "→ 打开记录  https://example.com/base/recXXXX",
        ],
    )
    frames.append((img, 1800))

    img = Image.new("RGB", (W, H))
    draw = draw_chrome(img)
    bubble_user(draw, 100, "Acme API 上线了吗")
    frames.append((img, 1200))

    img = Image.new("RGB", (W, H))
    draw = draw_chrome(img)
    y = bubble_user(draw, 100, "Acme API 上线了吗")
    bubble_typing(draw, y)
    frames.append((img, 800))

    img = Image.new("RGB", (W, H))
    draw = draw_chrome(img)
    y = bubble_user(draw, 100, "Acme API 上线了吗")
    bubble_bot(
        draw,
        y,
        [
            "找到 2 条相关记录",
            "1. Acme API 流式返回 — 状态：联调中 · P1",
            "2. Acme API 鉴权改造 — 状态：已上线 · P2",
        ],
    )
    frames.append((img, 2600))

    # hold final
    frames.append((img.copy(), 1400))
    return frames


def write_demo_gif() -> Path:
    frames = make_frames()
    out = ASSETS / "demo.gif"
    images = [im.convert("P", palette=Image.ADAPTIVE, colors=160) for im, _ in frames]
    durations = [d for _, d in frames]
    images[0].save(
        out,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=False,
        disposal=2,
    )
    return out


def main() -> None:
    svg = write_architecture_svg()
    gif = write_demo_gif()
    print(f"wrote {svg} ({svg.stat().st_size} bytes)")
    print(f"wrote {gif} ({gif.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
