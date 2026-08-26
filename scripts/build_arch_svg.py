#!/usr/bin/env python3
"""Generate a clean, layered SVG of the Coding Agent Harness architecture."""

W, H = 1200, 1180
CX = W // 2  # 600

# colour palette (class -> fill, stroke, text)
CLASS = {
    "entry":  ("#e3f2fd", "#1e88e5", "#0d47a1"),
    "loop":   ("#fff3e0", "#fb8c00", "#e65100"),
    "tool":   ("#e8f5e9", "#43a047", "#1b5e20"),
    "team":   ("#e0f7fa", "#26c6da", "#006064"),
    "safe":   ("#ffebee", "#e53935", "#b71c1c"),
    "ctx":    ("#f3e5f5", "#ab47bc", "#6a1b9a"),
    "mcp":    ("#fff8e1", "#f9a825", "#7a5b00"),
    "ext":    ("#eceff1", "#90a4ae", "#37474f"),
}


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def box(x_center, y_top, w, h, lines, cls, fs=15):
    """Return an SVG group: rounded rect + centered multi-line text."""
    fill, stroke, color = CLASS[cls]
    x, y = x_center - w / 2, y_top
    n = len(lines)
    parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" class="{cls}"/>']
    for i, line in enumerate(lines):
        # distribute lines around the vertical centre
        ty = y + h / 2 + (i - (n - 1) / 2) * (fs + 5)
        parts.append(f'<text x="{x_center}" y="{ty}" text-anchor="middle" dominant-baseline="middle" font-size="{fs}" fill="{color}">{esc(line)}</text>')
    return "".join(parts)


def v_arrow(x1, y1, y2, cls="flow"):
    return f'<line x1="{x1}" y1="{y1}" x2="{x1}" y2="{y2}" class="{cls}" marker-end="url(#arr)"/>'


def band(y, h, cls, header):
    fill, stroke, _ = CLASS["entry"]
    if cls == "band":
        fill, stroke = "#ffffff", "#e0e0e0"
    return (f'<rect x="24" y="{y}" width="{W-48}" height="{h}" rx="14" fill="{fill}" opacity="0.45" stroke="{stroke}" stroke-dasharray="3 3"/>'
            f'<text x="44" y="{y+24}" font-size="17" font-weight="700" fill="#455a64">{esc(header)}</text>')


out = []
out.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="ui-sans-serif, system-ui, -apple-system, Segoe UI, Arial, sans-serif" width="100%" height="auto">')
out.append(f'<title>Coding Agent Harness — System Architecture</title>')
style = []
for cls, (fill, stroke, color) in CLASS.items():
    style.append(f'.{cls}{{fill:{fill};stroke:{stroke};stroke-width:1.6}}')
    style.append(f'text.{cls}{{fill:{color}}}')
style.append('.flow{stroke:#546e7a;stroke-width:2.2}')
style.append('.cut{stroke:#ff7043;stroke-width:1.8;stroke-dasharray:6 4}')
style.append('text{font-size:15px}')
out.append('<style>' + "".join(style) + '</style>')
out.append(f'<defs><marker id="arr" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="#546e7a"/></marker>'
           f'<marker id="arrCut" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="#ff7043"/></marker></defs>')

# ---------------- Layer 1 : entry ----------------
b1y, b1h = 66, 160
out.append(band(b1y, b1h, "band", "① 入口 / 驱动（二选一）"))
e1y = b1y + 52
out.append(box(360, e1y, 300, 66, ["REPL 交互", "agent_loop()"], "entry", 16))
out.append(box(780, e1y, 300, 66, ["headless 评测", "bootstrap() / run_episode()"], "entry", 16))

# ---------------- Layer 2 : core loop ----------------
b2y, b2h = 258, 138
out.append(band(b2y, b2h, "loop", "② 核心主循环 agent_loop()"))
out.append(box(600, b2y + 46, 840, 54, ["调用 LLM   →   解析 tool_use   →   权限检查   →   执行工具   →   回写 tool_result   →   （回到调用）"], "loop", 16))

# ---------------- Layer 3 : tools ----------------
b3y, b3h = 428, 262
out.append(band(b3y, b3h, "band", "③ 工具分发表  TOOL_HANDLERS / TOOLS（37 种）"))
t1y = b3y + 46
row1 = [["bash / 读 / 写 /", "编辑 / 检索"], ["会话内 Todo", "磁盘任务板 Task"], ["子代理 task", "技能 load_skill"], ["文件消息总线", "TeammateManager"]]
row1cls = ["tool", "tool", "tool", "team"]
row2 = [["后台任务", "Cron 调度"], ["Git Worktree", "任务隔离"], ["mcp__{server}", "__{tool} 前缀路由"]]
row2cls = ["team", "team", "mcp"]
rx0 = [285, 495, 705, 915]
for i, (txt, cls) in enumerate(zip(row1, row1cls)):
    out.append(box(rx0[i], t1y, 170, 60, txt, cls, 14))
t2y = t1y + 74
rx1 = [390, 600, 810]
for i, (txt, cls) in enumerate(zip(row2, row2cls)):
    out.append(box(rx1[i], t2y, 170, 60, txt, cls, 14))

# ---------------- Layer 4 : shared facilities ----------------
b4y, b4h = 722, 138
out.append(band(b4y, b4h, "band", "④ 共享设施（每个工具都要经过）"))
s4y = b4y + 46
sx = [180, 390, 600, 810, 1020]
sfac = [([ "[ 统一权限门 ]", "PermissionManager"], "safe"),
        (["BashSecurity", "Validator"], "safe"),
        (["MemoryManager", "跨会话记忆"], "ctx"),
        (["microcompact", "/ auto_compact"], "ctx"),
        (["HookManager", "Pre / Post / SessionStart"], "ctx")]
for (txt, cls), x in zip(sfac, sx):
    out.append(box(x, s4y, 196, 60, txt, cls, 14))

# ---------------- Layer 5 : external ----------------
b5y, b5h = 892, 160
out.append(band(b5y, b5h, "band", "⑤ 外部接入"))
e5y = b5y + 50
out.append(box(240, e5y, 300, 66, ["MCP 服务器", "mcp_plugin.py · stdio"], "mcp", 15))
out.append(box(600, e5y, 300, 66, ["Anthropic 兼容端点", "可切换任意 LLM"], "ext", 15))
out.append(box(960, e5y, 300, 66, ["子进程沙箱", "Git Worktree"], "ext", 15))

# ---------------- vertical flow arrows ----------------
out.append(v_arrow(600, b1y + b1h, b2y))
out.append(v_arrow(600, b2y + b2h, b3y))
out.append(v_arrow(600, b3y + b3h, b4y))
out.append(v_arrow(600, b4y + b4h, b5y))

# ---------------- legend ----------------
ly = 1130
legend = [("entry", "入口"), ("loop", "主循环"), ("tool", "工具"), ("team", "多 Agent"),
          ("safe", "安全"), ("ctx", "上下文/记忆"), ("mcp", "MCP"), ("ext", "外部")]
lx = 60
out.append(f'<line x1="24" y1="{ly-20}" x2="{W-24}" y2="{ly-20}" stroke="#cfd8dc"/>')
out.append(f'<text x="44" y="{ly}" font-size="14" font-weight="600" fill="#455a64">图例：</text>')
lx = 120
for cls, name in legend:
    fill = CLASS[cls][0]
    out.append(f'<rect x="{lx}" y="{ly-16}" width="20" height="20" rx="4" fill="{fill}" stroke="{CLASS[cls][1]}" stroke-width="1.4"/>')
    out.append(f'<text x="{lx+26}" y="{ly}" font-size="14" fill="#455a64">{esc(name)}</text>')
    lx += 34 + len(name) * 15 + 8

out.append([""][0] if False else "")
out.append('</svg>')

svg = "\n".join(out)
with open("docs/diagrams/architecture.svg", "w", encoding="utf-8") as f:
    f.write(svg)
print("wrote", len(svg), "bytes")
