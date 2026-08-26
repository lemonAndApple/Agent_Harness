#!/usr/bin/env python3
"""Generate vector SVG diagrams for the repo into docs/diagrams/.

Produces:
  * architecture.svg      -> 5-layer system architecture (README + DESIGN.md)
  * loop-sequence.svg     -> agent-loop interaction sequence (DESIGN.md)
  * data-pipeline.svg     -> data synthesis -> QC -> iteration flow (DATA_PIPELINE.md)

Run from the repo root:  python3 scripts/build_diagrams.py
"""

# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
import os

OUT = "docs/diagrams"
os.makedirs(OUT, exist_ok=True)

# class -> (fill, stroke, text-colour)
CLASS = {
    "entry": ("#e3f2fd", "#1e88e5", "#0d47a1"),
    "loop":  ("#fff3e0", "#fb8c00", "#e65100"),
    "tool":  ("#e8f5e9", "#43a047", "#1b5e20"),
    "team":  ("#e0f7fa", "#26c6da", "#006064"),
    "safe":  ("#ffebee", "#e53935", "#b71c1c"),
    "ctx":   ("#f3e5f5", "#ab47bc", "#6a1b9a"),
    "mcp":   ("#fff8e1", "#f9a825", "#7a5b00"),
    "ext":   ("#eceff1", "#90a4ae", "#37474f"),
}


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def svg_head(title, w, h):
    sty = []
    for cls, (fill, stroke, color) in CLASS.items():
        sty.append(f'.{cls}{{fill:{fill};stroke:{stroke};stroke-width:1.6}}')
        sty.append(f'text.{cls}{{fill:{color}}}')
    sty.append('.flow{stroke:#546e7a;stroke-width:2.2}')
    sty.append('.soft{stroke:#78909c;stroke-width:1.6;stroke-dasharray:5 4}')
    sty.append('.edge{font-size:13px;fill:#37474f}')
    sty.append('text{font-size:15px}')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'font-family="ui-sans-serif, system-ui, -apple-system, Segoe UI, Arial, sans-serif" width="100%" height="auto">\n'
        f'<title>{esc(title)}</title>\n'
        f'<style>{"".join(sty)}</style>\n'
        f'<defs>'
        f'<marker id="arr" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="#546e7a"/></marker>'
        f'<marker id="arrO" markerWidth="11" markerHeight="11" refX="7" refY="3.5" orient="auto"><path d="M0,0 L8,3.5 L0,7 Z" fill="none" stroke="#546e7a"/></marker>'
        f'</defs>\n'
    )


def rect_box(xc, y, w, h, lines, cls, fs=15):
    fill, stroke, color = CLASS[cls]
    x = xc - w / 2
    n = len(lines)
    out = [f'<rect x="{x:g}" y="{y}" width="{w}" height="{h}" rx="9" class="{cls}"/>']
    for i, line in enumerate(lines):
        ty = y + h / 2 + (i - (n - 1) / 2) * (fs + 5)
        out.append(f'<text x="{xc:g}" y="{ty:g}" text-anchor="middle" dominant-baseline="middle" '
                   f'font-size="{fs}" fill="{color}">{esc(line)}</text>')
    return "".join(out)


def write(name, body):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"wrote {path} ({len(body)} bytes)")


# --------------------------------------------------------------------------- #
# 1) architecture.svg   (5 layered system architecture)
# --------------------------------------------------------------------------- #
def architecture():
    W, H = 1200, 1180
    o = [svg_head("Coding Agent Harness — System Architecture", W, H)]

    def band(y, h, tint, header):
        fill = CLASS[tint][0] if tint != "band" else "#ffffff"
        stroke = CLASS[tint][1] if tint != "band" else "#e0e0e0"
        o.append(f'<rect x="24" y="{y}" width="{W-48}" height="{h}" rx="14" fill="{fill}" opacity="0.45" stroke="{stroke}" stroke-dasharray="3 3"/>')
        o.append(f'<text x="44" y="{y+24}" font-size="17" font-weight="700" fill="#455a64">{esc(header)}</text>')

    def varrow(y1, y2, x=600):
        o.append(f'<line x1="{x}" y1="{y1}" x2="{x}" y2="{y2}" class="flow" marker-end="url(#arr)"/>')

    # layer 1
    band(66, 160, "band", "① 入口 / 驱动（二选一）")
    o.append(rect_box(360, 118, 300, 66, ["REPL 交互", "agent_loop()"], "entry", 16))
    o.append(rect_box(780, 118, 300, 66, ["headless 评测", "bootstrap() / run_episode()"], "entry", 16))
    # layer 2
    band(258, 138, "loop", "② 核心主循环 agent_loop()")
    o.append(rect_box(600, 304, 840, 54, ["调用 LLM   →   解析 tool_use   →   权限检查   →   执行工具   →   回写 tool_result   →   （回到调用）"], "loop", 16))
    # layer 3
    band(428, 262, "band", "③ 工具分发表  TOOL_HANDLERS / TOOLS（37 种）")
    row1 = [(["bash / 读 / 写 /", "编辑 / 检索"], "tool"),
            (["会话内 Todo", "磁盘任务板 Task"], "tool"),
            (["子代理 task", "技能 load_skill"], "tool"),
            (["文件消息总线", "TeammateManager"], "team")]
    row2 = [(["后台任务", "Cron 调度"], "team"),
            (["Git Worktree", "任务隔离"], "team"),
            (["mcp__{server}", "__{tool} 前缀路由"], "mcp")]
    for xc, (txt, cls) in zip([285, 495, 705, 915], row1):
        o.append(rect_box(xc, 474, 170, 60, txt, cls, 14))
    for xc, (txt, cls) in zip([390, 600, 810], row2):
        o.append(rect_box(xc, 548, 170, 60, txt, cls, 14))
    # layer 4
    band(722, 138, "band", "④ 共享设施（每个工具都要经过）")
    s4 = [(["[ 统一权限门 ]", "PermissionManager"], "safe"),
          (["BashSecurity", "Validator"], "safe"),
          (["MemoryManager", "跨会话记忆"], "ctx"),
          (["microcompact", "/ auto_compact"], "ctx"),
          (["HookManager", "Pre / Post / SessionStart"], "ctx")]
    for xc, (txt, cls) in zip([180, 390, 600, 810, 1020], s4):
        o.append(rect_box(xc, 768, 196, 60, txt, cls, 14))
    # layer 5
    band(892, 160, "band", "⑤ 外部接入")
    o.append(rect_box(240, 942, 300, 66, ["MCP 服务器", "mcp_plugin.py · stdio"], "mcp", 15))
    o.append(rect_box(600, 942, 300, 66, ["Anthropic 兼容端点", "可切换任意 LLM"], "ext", 15))
    o.append(rect_box(960, 942, 300, 66, ["子进程沙箱", "Git Worktree"], "ext", 15))
    # arrows
    varrow(226, 258); varrow(396, 428); varrow(690, 722); varrow(860, 892)
    # legend
    ly = 1130
    o.append(f'<line x1="24" y1="{ly-20}" x2="{W-24}" y2="{ly-20}" stroke="#cfd8dc"/>')
    o.append(f'<text x="44" y="{ly}" font-size="14" font-weight="600" fill="#455a64">图例：</text>')
    lx = 120
    for cls, name in [("entry", "入口"), ("loop", "主循环"), ("tool", "工具"), ("team", "多 Agent"),
                      ("safe", "安全"), ("ctx", "上下文/记忆"), ("mcp", "MCP"), ("ext", "外部")]:
        fill = CLASS[cls][0]
        o.append(f'<rect x="{lx}" y="{ly-16}" width="20" height="20" rx="4" fill="{fill}" stroke="{CLASS[cls][1]}" stroke-width="1.4"/>')
        o.append(f'<text x="{lx+26}" y="{ly}" font-size="14" fill="#455a64">{esc(name)}</text>')
        lx += 34 + len(name) * 15 + 8
    o.append('</svg>')
    write("architecture.svg", "\n".join(o))


# --------------------------------------------------------------------------- #
# 2) loop-sequence.svg   (agent-loop interaction sequence)
# --------------------------------------------------------------------------- #
def loop_sequence():
    W, H = 1100, 780
    o = [svg_head("Coding Agent Harness — Main Loop", W, H)]
    px = {"U": 130, "A": 360, "G": 570, "T": 790, "L": 985}
    top, l0, l1 = 60, 120, 720
    parts = [("U", "用户 / 评测驱动"), ("A", "主循环 agent_loop()"),
             ("G", "PermissionManager"), ("T", "工具（bash/文件/MCP…）"),
             ("L", "LLM（Anthropic 兼容）")]
    for key, label in parts:
        x = px[key]
        o.append(f'<rect x="{x-85}" y="{top}" width="170" height="40" rx="8" class="entry"/>')
        o.append(f'<text x="{x}" y="{top+20}" text-anchor="middle" dominant-baseline="middle" font-size="14" fill="#0d47a1">{esc(label)}</text>')
        o.append(f'<line x1="{x}" y1="{l0}" x2="{x}" y2="{l1}" class="soft"/>')
    # activation bar on A
    o.append(f'<rect x="{px["A"]-4}" y="180" width="8" height="520" rx="3" fill="#fb8c00" opacity="0.35"/>')

    # message arrows: (from,to,text, y, is_return)
    labels = {"U": px["U"], "A": px["A"], "G": px["G"], "T": px["T"], "L": px["L"]}
    msgs = [("U", "A", "输入任务", 150, False),
            ("A", "L", "调 LLM（上传对话历史）", 210, False),
            ("L", "A", "assistant（含 tool_use）", 250, True)]

    def edge(a, b, txt, y, ret):
        xa, xb = labels[a], labels[b]
        cls = "soft" if ret else "flow"
        marker = "url(#arrO)" if ret else "url(#arr)"
        o.append(f'<line x1="{xa}" y1="{y}" x2="{xb}" y2="{y}" class="{cls}" marker-end="{marker}"/>')
        tx = (xa + xb) / 2
        o.append(f'<text x="{tx:g}" y="{y-8}" text-anchor="middle" class="edge">{esc(txt)}</text>')

    for a, b, t, y, r in msgs:
        edge(a, b, t, y, r)

    # loop frame
    o.append(f'<rect x="{px["A"]-40}" y="286" width="{W - px["A"] - 20}" height="330" rx="10" fill="#fff8e1" stroke="#f9a825"/>')
    o.append(f'<text x="{px["A"]-30}" y="308" font-size="14" font-weight="700" fill="#7a5b00">loop  每步工具调用</text>')
    loop_msgs = [("A", "G", "校验 tool_use", 336, False),
                 ("G", "A", "allow / deny / ask_user", 376, True),
                 ("A", "T", "执行工具", 416, False),
                 ("T", "A", "tool_result（超长输出落盘 + 预览）", 456, True),
                 ("A", "L", "续接（回填 tool_result）", 496, False),
                 ("L", "A", "下一条回复", 536, True)]
    for a, b, t, y, r in loop_msgs:
        edge(a, b, t, y, r)

    # final
    edge("A", "U", "最终回答", 690, False)
    o.append('</svg>')
    write("loop-sequence.svg", "\n".join(o))


# --------------------------------------------------------------------------- #
# 3) data-pipeline.svg   (synthesis -> QC -> iteration flow)
# --------------------------------------------------------------------------- #
def data_pipeline():
    W, H = 1000, 870
    o = [svg_head("Data Synthesis → QC → Iteration", W, H)]
    xc = 500
    bw, bh = 520, 104
    nodes = [
        (["SWE-bench 失败实例", "官方判定 FAIL"], "safe", 40),
        (["任务1 · synth_negatives.py", "构造错误对比对（bad ⇄ 修正 good）+ 分类"], "tool", 200),
        (["任务2 · synth_rubric.py", "生成 rubric + LLM-as-judge 判定 + 质控统计"], "tool", 360),
        (["质量检查", "schema 校验 / 去重 / 可追溯 / 抽样质检"], "ctx", 520),
        (["迭代验证", "基线 vs 增广 前后对比，报告 Δ + 方差"], "team", 680),
    ]
    for lines, cls, y in nodes:
        o.append(rect_box(xc, y, bw, bh, lines, cls, 15))
    for y in range(40, 680, 160):
        o.append(f'<line x1="{xc}" y1="{y+bh}" x2="{xc}" y2="{y+160}" class="flow" marker-end="url(#arr)"/>')
    o.append(f'<text x="60" y="{H-26}" font-size="13" fill="#546e7a">'
             f'注：全程仅用 agent 失败 patch，gold patch 零注入；</text>')
    o.append(f'<text x="60" y="{H-6}" font-size="13" fill="#546e7a">'
             f'迭代验证必须做“基线 vs 增广”的配对对比并报告方差，当前已设计、待预算运行。</text>')
    o.append('</svg>')
    write("data-pipeline.svg", "\n".join(o))


if __name__ == "__main__":
    architecture()
    loop_sequence()
    data_pipeline()
