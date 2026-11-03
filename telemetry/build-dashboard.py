#!/usr/bin/env python3
"""
Build telemetry/grafana/aifabric-topology.json: the aifabric GPU backend as ONE live flow panel.

Owner 2026-10-26: "build a grafana dashboard for this lab, only the dynamic topology ... similar
to the upgradepipeline demo grafana". House style (telemetry-stack §0): white canvas, draw.io
device icons, every link split into two directional halves (arrow + live rate pill), port dots
= oper-state. Primitives copied from eda/scenarios/upgrade-pipeline/telemetry/build-dashboard.py.

What is drawn (topology/gen-topology.py is the source of the port plan):
  ONE aifabrics Backend over all 8 switches, EVPN overlay (owner 2026-10-27; 10-26 → 10-27 l1-5/l1-6 were
  their own Fabric frontend):
  spines   l2-1, l2-2                     Backend stripe connector
  leaves   l1-1..l1-4 = rails 1-4         Backend stripe gpu1  l1-5, l1-6  Backend stripe non-gpu (storage,
                                          CPU, management nodes; no GPU ports)
  ISLs     84 = 12 bundles (leaf x spine) of 7 x 800G, all Backend ISLs. leaf e1-51..57 -> l2-1, e1-58..64 -> l2-2.
           Every ISL is one thin STRAND, each half coloured by that ISL's own rate in that
           direction (DLB/ECMP spread = all 7 strands lit); per bundle one arrow + pill per
           direction = bundle sum, and the leaf-end badge = ISLs up (7 = all).
  GPU      4 servers x 4 rails: server s rail r = l1-r ethernet-1/<ceil(s/2)>/<(s-1)%2+1> = GPU(r-1)
  storage  9 dual-homed island hosts (ESI-LAG l1-5 + l1-6, ethernet-1/1/x, 1/11/x, 1/12/x)
  tenant   PER GPU (owner 2026-10-26: tenants book GPUs, not servers; Q6 A): each server card is the
           neutral physical blade with 4 GPU tiles, one per GPU = one NIC = one rail; a tile is coloured
           and labelled live by the `tenant` label of that NIC's EDA Interface (Export aifabric-tenant),
           grey "free" when unbooked. cpu-1 = shared CPU node, no tenant (owner Q1 B 2026-10-27). The legend counts the
           GPUs per tenant live. A booking / release / move shows up within one scrape.
Servers are Linux sims without metrics => every server cable binds to the LEAF port:
leaf->server = leaf :out, server->leaf = leaf :in, server-side dot = leaf oper-state.

Usage: python3 telemetry/build-dashboard.py            (deploy: bash deploy.sh --phase grafana)
       python3 telemetry/build-dashboard.py --check    every dataRef against live Prometheus
"""
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "grafana" / "aifabric-topology.json"
ICON_SWITCH = (HERE / "icon_switch.svg").read_text()
ICON_HOST = (HERE / "icon_host.svg").read_text()
NS = "aifabric"
DS = {"type": "prometheus", "uid": "aif-prom"}
FONT = "Inter,Segoe UI,Helvetica,Arial,sans-serif"

# ---- canvas / palette -------------------------------------------------------
W, H = 2000, 960
INK, MUTED = "#0f172a", "#64748b"
C_SPINE, C_LEAF, C_STLEAF = "#2c3e50", "#e67e22", "#16a085"
C_GPU, C_HOST = "#76b900", "#64748b"
# tenant colours: (tile tint, strong) — index = tenant number, 0 = free (no tenant label)
TENANT = {0: ("#f1f5f9", "#94a3b8"), 1: ("#dbeafe", "#2563eb"), 2: ("#ffedd5", "#ea580c"),
          3: ("#ede9fe", "#7c3aed")}
S_SW, S_HOST, S_GPU = 64, 34, 46
Y_SPINE, Y_LEAF, Y_HOST = 150, 452, 800
SPINES = {"l2-1": 700, "l2-2": 1300}
RAIL_LEAVES = {"l1-1": 230, "l1-2": 500, "l1-3": 770, "l1-4": 1040}
ST_LEAVES = {"l1-5": 1450, "l1-6": 1760}
LEAVES = {**RAIL_LEAVES, **ST_LEAVES}
ISL_PER_SPINE = 7
GPU_SERVERS = {"gpu-1": 230, "gpu-2": 500, "gpu-3": 770, "gpu-4": 1040}
NIC_OFF = {1: -81, 2: -27, 3: 27, 4: 81}          # GPU0..GPU3 = rail 1..4 along the card top
CARD_W, CARD_TOP, CARD_BOT = 244, 730, 906
# storage island: host -> (x, port, channel, role caption)
ISLAND = {
    "ces-1": (1250, 1, 1, "storage"), "axelera-1": (1335, 1, 2, "accelerator"),
    "ocp-m1": (1420, 11, 1, "ocp master"), "ocp-m2": (1505, 11, 2, "ocp master"),
    "ocp-m3": (1590, 11, 3, "ocp master"), "ocp-s1": (1675, 11, 4, "ocp services"),
    "ocp-s2": (1760, 12, 1, "ocp services"), "ocp-s3": (1845, 12, 2, "ocp services"),
    "cpu-1": (1930, 12, 3, "shared cpu"),
}


# ---- svg primitives (upgrade-pipeline / microseg build_dashboard.py) -----------
def place_icon(template, uid, fill, cx, cy, size):
    """Stamp a draw.io glyph. The id must be unique per node: the icon's CSS is keyed on it."""
    icon = template
    old = re.search(r'id="(svg-image-[^"]+)"', icon).group(1)
    icon = icon.replace(old, uid)
    icon = re.sub(r"(\.st0 \{ fill: )[^;]+;", r"\g<1>%s;" % fill, icon, count=1)
    op = icon.find(">", icon.find('<svg version="1.1"'))
    head, rest = icon[: op + 1], icon[op + 1:]
    x, y = cx - size / 2, cy - size / 2
    head = re.sub(r' x="[^"]*"', f' x="{x:.1f}"', head, count=1)
    head = re.sub(r' y="[^"]*"', f' y="{y:.1f}"', head, count=1)
    head = re.sub(r' width="[^"]*"', f' width="{size:.1f}"', head, count=1)
    head = re.sub(r' height="[^"]*"', f' height="{size:.1f}"', head, count=1)
    return head + rest


def text(x, y, s, size=14, fill=INK, weight="600", anchor="middle"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{s}</text>')


def pill_html(lx, ly, size=10, word="rate", extra=""):
    """foreignObject 'pill' centred on (lx, ly); the plugin replaces the word with the live value."""
    return ('<g transform="translate(-0.5 -0.5)"><switch><foreignObject style="overflow: visible; text-align: left;" '
            'pointer-events="none" width="100%" height="100%" requiredFeatures="http://www.w3.org/TR/SVG11/feature#Extensibility">'
            '<div xmlns="http://www.w3.org/1999/xhtml" style="display: flex; align-items: unsafe center; justify-content: unsafe center; '
            f'width: 1px; height: 1px; padding-top: {ly:.0f}px; margin-left: {lx:.0f}px;">'
            '<div style="box-sizing: border-box; font-size: 0; text-align: center;">'
            f'<div style="display: inline-block; font-size: {size}px; font-weight: 600; color: #0f172a; line-height: 1.2; '
            'pointer-events: all; background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 3px; '
            f'padding: 0px 3px; white-space: nowrap;{extra}">{word}</div></div></div></foreignObject></switch></g>')


def arrow_head(x1, y1, x2, y2, size=7):
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    if L == 0:
        return "", x2, y2
    ux, uy = dx / L, dy / L
    bx, by = x2 - ux * size, y2 - uy * size
    wx, wy = -uy, ux
    half = size * 0.55
    w1 = (bx + wx * half, by + wy * half)
    w2 = (bx - wx * half, by - wy * half)
    return (f"M {w1[0]:.1f} {w1[1]:.1f} L {x2:.1f} {y2:.1f} L {w2[0]:.1f} {w2[1]:.1f} Z", bx, by)


# ---- pill placement: slide each pill along its half until it overlaps nothing ----
PILL_W, PILL_H = 62, 21
obstacles = []      # (x0, y0, x1, y1) boxes pills must avoid (icons, labels)
placed = []         # boxes of pills already placed


def box_overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def place_pill(x1, y1, mx, my, pref=0.55, lo=0.2, hi=0.95, dy=0.0):
    """Pill on the half from (x1,y1) to its midpoint: lfrac 0 = at the node, 1 = at the midpoint.
    Picks the lfrac closest to `pref` with the least overlap; returns the centre."""
    best = None
    steps = 31
    for i in range(steps):
        f = lo + (hi - lo) * i / (steps - 1)
        cx, cy = x1 + (mx - x1) * f, y1 + (my - y1) * f + dy
        b = (cx - PILL_W / 2, cy - PILL_H / 2, cx + PILL_W / 2, cy + PILL_H / 2)
        score = sum(box_overlap(b, p) for p in placed) * 10 + sum(box_overlap(b, o) for o in obstacles) * 4
        score += abs(f - pref) * 40
        if best is None or score < best[0]:
            best = (score, cx, cy, b)
    placed.append(best[3])
    return best[1], best[2]


links_svg, strands_svg, ports_svg, overlay_svg, pills_svg, cells = [], [], [], [], [], []

MID_ELLIPSE = ('<g id="cell-mid:{A}:{AIF}:{B}:{BIF}" data-label=""><ellipse cx="{MX:.1f}" cy="{MY:.1f}" '
               'rx="2.5" ry="2.5" fill="#94a3b8" stroke="none"/></g>')
PORT_FILL = ('<g id="cell-{A}:{AIF}:{B}:{BIF}"><ellipse cx="{X:.1f}" cy="{Y:.1f}" rx="{R}" ry="{R}" '
             'fill="#94a3b8" stroke="#475569" stroke-width="1"/></g>')


def half_link(cid, a_ref, mid_ref, x1, y1, ex, ey, arrow, width=4):
    return (f'<g id="cell-{cid}" data-source="{a_ref}" data-target="{mid_ref}">'
            f'<path d="M {x1:.1f} {y1:.1f} L {ex:.1f} {ey:.1f}" fill="none" stroke="#cbd5e1" stroke-width="{width}" '
            'stroke-linecap="round" pointer-events="stroke"/>'
            f'<path d="{arrow}" fill="#cbd5e1" stroke="#cbd5e1" stroke-width="1"/></g>')


def make_link(a, aif, b, bif, p1, p2, fwd, rev, a_oper, b_oper, pills=True, pref=0.55, pref2=None,
              width=4, port_r=5.5):
    """1 midpoint + 2 half-segments (+ pills) + 2 port dots, both halves pointing at the midpoint."""
    (x1, y1), (x2, y2) = p1, p2
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    mid = f"mid:{a}:{aif}:{b}:{bif}"
    links_svg.append(MID_ELLIPSE.format(A=a, AIF=aif, B=b, BIF=bif, MX=mx, MY=my))
    for (sx, sy, sa, saif, sb, sbif, pr, ref) in (
            (x1, y1, a, aif, b, bif, pref, fwd),
            (x2, y2, b, bif, a, aif, pref if pref2 is None else pref2, rev)):
        ar, ex, ey = arrow_head(sx, sy, mx, my)
        links_svg.append(half_link(f"link_id:{sa}:{saif}:{sb}:{sbif}", f"{sa}:{saif}:{sb}:{sbif}", mid,
                                   sx, sy, ex, ey, ar, width))
        if pills:
            # the pill is its own cell on a top layer (same dataRef), so no later cable covers it
            pxy = place_pill(sx, sy, mx, my, pref=pr)
            pills_svg.append(f'<g id="cell-rate:{sa}:{saif}:{sb}:{sbif}">' + pill_html(*pxy) + "</g>")
            cells.append((f"rate:{sa}:{saif}:{sb}:{sbif}", ref, "rate"))
    ports_svg.append(PORT_FILL.format(A=a, AIF=aif, B=b, BIF=bif, X=x1, Y=y1, R=port_r))
    ports_svg.append(PORT_FILL.format(A=b, AIF=bif, B=a, BIF=aif, X=x2, Y=y2, R=port_r))
    cells.append((f"link_id:{a}:{aif}:{b}:{bif}", fwd, "traffic"))
    cells.append((f"link_id:{b}:{bif}:{a}:{aif}", rev, "traffic"))
    cells.append((f"{a}:{aif}:{b}:{bif}", a_oper, "oper"))
    cells.append((f"{b}:{bif}:{a}:{aif}", b_oper, "oper"))


def edge(sw, swif, host, hif, p1, p2, **kw):
    """leaf<->server: the server has no metrics, so server->leaf = leaf INBOUND."""
    make_link(sw, swif, host, hif, p1, p2, fwd=f"{sw}:{swif}:out", rev=f"{sw}:{swif}:in",
              a_oper=f"oper-state:{sw}:{swif}", b_oper=f"oper-state:{sw}:{swif}", **kw)


# ---- node boxes first, so pills keep clear of icons and labels -----------------
def node_box(cx, cy, size, pad=4):
    return (cx - size / 2 - pad, cy - size / 2 - pad, cx + size / 2 + pad, cy + size / 2 + pad)


def label_box(x, y, txt, size, anchor):
    w = len(txt) * size * 0.58
    x0 = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
    return (x0, y - size, x0 + w, y + 4)


for x in SPINES.values():
    obstacles.append(node_box(x, Y_SPINE, S_SW))
for x in LEAVES.values():
    obstacles.append(node_box(x, Y_LEAF, S_SW))
for x in GPU_SERVERS.values():
    obstacles.append((x - CARD_W / 2, CARD_TOP + 10, x + CARD_W / 2, CARD_BOT))
for (x, *_r) in ISLAND.values():
    obstacles.append(node_box(x, Y_HOST, S_HOST, pad=8))
for n, x in SPINES.items():
    side, anchor = (-1, "end") if n == "l2-1" else (1, "start")
    tx = x + side * (S_SW / 2 + 12 + 60)
    obstacles.append(label_box(tx, Y_SPINE - 4, n, 16, anchor))
    obstacles.append(label_box(tx, Y_SPINE + 13, "spine · 7220 IXR-H5-64D", 10, anchor))
for n, x in LEAVES.items():
    obstacles.append(label_box(x - S_SW / 2 - 10, Y_LEAF + 2, n, 15, "end"))
    obstacles.append(label_box(x - S_SW / 2 - 10, Y_LEAF + 17, "non-gpu", 10, "end"))

# ═══════════ ISL bundles: 7 strands per leaf x spine ═══════════
STRAND_GAP = 3.2
for k, (leaf, lx0) in enumerate(LEAVES.items(), 1):
    for si, (spine, sx0) in enumerate(SPINES.items()):
        sx = sx0 + (k - 3.5) * 17                     # spread the 6 bundles along the spine
        lx = lx0 + (-17 if si == 0 else 17)           # l2-1 bundle left, l2-2 bundle right
        p_sp, p_lf = (sx, Y_SPINE + S_SW / 2 + 2), (lx, Y_LEAF - S_SW / 2 - 2)
        dx, dy = p_sp[0] - p_lf[0], p_sp[1] - p_lf[1]
        L = math.hypot(dx, dy)
        nx, ny = -dy / L, dx / L                      # unit normal of the bundle
        for j in range(ISL_PER_SPINE):
            lport = 51 + si * ISL_PER_SPINE + j
            off = (j - (ISL_PER_SPINE - 1) / 2) * STRAND_GAP
            ax, ay = p_lf[0] + nx * off, p_lf[1] + ny * off
            bx, by = p_sp[0] + nx * off, p_sp[1] + ny * off
            mx, my = (ax + bx) / 2, (ay + by) / 2
            lif = f"ethernet-1/{lport}"
            for (x1, y1, cid, ref) in ((ax, ay, f"isl:{leaf}:{lif}:up", f"{leaf}:{lif}:out"),
                                       (bx, by, f"isl:{leaf}:{lif}:down", f"{leaf}:{lif}:in")):
                strands_svg.append(f'<g id="cell-{cid}"><path d="M {x1:.1f} {y1:.1f} L {mx:.1f} {my:.1f}" '
                                   'fill="none" stroke="#cbd5e1" stroke-width="2.2" stroke-linecap="butt"/></g>')
                cells.append((cid, ref, "strand"))
        # bundle overlay: arrow + pill per direction (sum of the 7), badge = ISLs up
        mx, my = (p_lf[0] + p_sp[0]) / 2, (p_lf[1] + p_sp[1]) / 2
        for (x1, y1, d, pref) in ((p_lf[0], p_lf[1], "out", 0.62), (p_sp[0], p_sp[1], "in", 0.55)):
            ar, _, _ = arrow_head(x1, y1, mx, my, size=13)
            pxy = place_pill(x1, y1, mx, my, pref=pref, lo=0.3, hi=0.9)
            cid = f"bundle:{leaf}:{spine}:{'up' if d == 'out' else 'down'}"
            overlay_svg.append(f'<g id="cell-{cid}"><path d="{ar}" fill="#cbd5e1" stroke="#ffffff" stroke-width="1.5"/>'
                               + pill_html(*pxy) + "</g>")
            cells.append((cid, f"bundle:{leaf}:{spine}:{d}", "bundle"))
        bx_, by_ = p_lf[0], p_lf[1] - 2
        cid = f"bundle-up:{leaf}:{spine}"
        ports_svg.append(f'<g id="cell-{cid}"><ellipse cx="{bx_:.1f}" cy="{by_:.1f}" rx="8" ry="8" fill="#94a3b8" '
                         'stroke="#475569" stroke-width="1"/>'
                         + pill_html(bx_, by_, size=9, word="n", extra=" background-color: transparent; border: none; padding: 0;")
                         + "</g>")
        cells.append((cid, f"bundle-up:{leaf}:{spine}", "count"))
        ports_svg.append(f'<g id="cell-{cid}:spine"><ellipse cx="{p_sp[0]:.1f}" cy="{p_sp[1]:.1f}" rx="5" ry="5" '
                         'fill="#94a3b8" stroke="#475569" stroke-width="1"/></g>')
        cells.append((f"{cid}:spine", f"bundle-up:{leaf}:{spine}", "count-dot"))

# ═══════════ GPU rails: leaf r -> GPU(r-1) of every server ═══════════
for s, (srv, gx) in enumerate(GPU_SERVERS.items(), 1):
    port = f"ethernet-1/{(s + 1) // 2}/{(s - 1) % 2 + 1}"
    for r, (leaf, lx) in enumerate(RAIL_LEAVES.items(), 1):
        nx_ = gx + NIC_OFF[r]
        lxa = lx + (s - 2.5) * 11                  # spread the 4 server cables along the leaf
        edge(leaf, port, srv, f"eth{r}", (lxa, Y_LEAF + S_SW / 2 + 2), (nx_, CARD_TOP + 2),
             pref=0.5, pref2=0.45, width=3.5)

# ═══════════ storage island: every host dual-homed to l1-5 + l1-6 ═══════════
for host, (hx, port, ch, role) in ISLAND.items():
    pif = f"ethernet-1/{port}/{ch}"
    for k, (leaf, lx) in enumerate(ST_LEAVES.items()):
        idx = list(ISLAND).index(host)
        lxa = lx + (idx - 4) * 6
        edge(leaf, pif, host, f"eth{k + 1}", (lxa, Y_LEAF + S_SW / 2 + 2),
             (hx + (-6 if k == 0 else 6), Y_HOST - S_HOST / 2 - 2), pills=False, width=2.5, port_r=4)

# ═══════════ nodes ═══════════
bg, nodes = [], []
# areas (static): Backend stripe gpu1 | Backend stripe non-gpu (l1-5/l1-6 + storage-island hosts)
bg.append('<rect x="100" y="378" width="1080" height="548" rx="14" fill="#f8fafc" stroke="#e2e8f0" stroke-width="1.5"/>')
bg.append('<rect x="1205" y="378" width="775" height="548" rx="14" fill="#f8fafc" stroke="#e2e8f0" stroke-width="1.5"/>')
HALO = ' stroke="#f8fafc" stroke-width="5" paint-order="stroke" stroke-linejoin="round"'
deco_top = [text(116, 398, "GPU backend · stripe gpu1 · rail-optimised", size=12, fill=MUTED, weight="700", anchor="start")
            .replace("<text ", f"<text{HALO} ", 1),
            text(1221, 398, "stripe non-gpu · storage, CPU, management · ESI-LAG", size=12, fill=MUTED, weight="700",
                 anchor="start").replace("<text ", f"<text{HALO} ", 1)]
bg.append(text(1000, 92, "spines · 84 ISLs, one Backend (7 × 800G per leaf per spine)", size=12, fill=MUTED,
               weight="600"))

for n, x in SPINES.items():
    nodes.append(place_icon(ICON_SWITCH, f"svg-image-{n}", C_SPINE, x, Y_SPINE, S_SW))
    side, anchor = (-1, "end") if n == "l2-1" else (1, "start")
    tx = x + side * (S_SW / 2 + 12 + 60)
    nodes.append(text(tx, Y_SPINE - 4, n, size=16, anchor=anchor))
    nodes.append(text(tx, Y_SPINE + 13, "spine · 7220 IXR-H5-64D", size=10, fill=MUTED, weight="600", anchor=anchor))
for r, (n, x) in enumerate(RAIL_LEAVES.items(), 1):
    nodes.append(place_icon(ICON_SWITCH, f"svg-image-{n}", C_LEAF, x, Y_LEAF, S_SW))
    nodes.append(text(x - S_SW / 2 - 10, Y_LEAF + 2, n, size=15, anchor="end"))
    nodes.append(text(x - S_SW / 2 - 10, Y_LEAF + 17, f"rail {r}", size=10, fill=C_LEAF, weight="700", anchor="end"))
for n, x in ST_LEAVES.items():
    nodes.append(place_icon(ICON_SWITCH, f"svg-image-{n}", C_STLEAF, x, Y_LEAF, S_SW))
    nodes.append(text(x - S_SW / 2 - 10, Y_LEAF + 2, n, size=15, anchor="end"))
    nodes.append(text(x - S_SW / 2 - 10, Y_LEAF + 17, "non-gpu", size=10, fill=C_STLEAF, weight="700", anchor="end"))

# GPU server cards = the physical blade (static, neutral) with one live tile per GPU: tile fill + tenant pill
# are data cells keyed by (server, rail) = the GPU's NIC Interface (owner Q6 A). Tile = GPU<G> = VM gpu-<s>-g<G>.
TILE_W, TILE_H = 50, 66
for s, (srv, gx) in enumerate(GPU_SERVERS.items(), 1):
    x0 = gx - CARD_W / 2
    bg.append(f'<rect x="{x0:.1f}" y="{CARD_TOP}" width="{CARD_W}" height="{CARD_BOT - CARD_TOP}" rx="10" '
              'fill="#ffffff" stroke="#cbd5e1" stroke-width="1.2"/>')
    for r, off in NIC_OFF.items():
        tx = gx + off
        bg.append(f'<g id="cell-tenant-tile:{srv}:{r}"><rect x="{tx - TILE_W / 2:.1f}" y="{CARD_TOP + 6}" width="{TILE_W}" '
                  f'height="{TILE_H}" rx="7" fill="{TENANT[0][0]}" stroke="#94a3b8" stroke-width="1"/></g>')
        cells.append((f"tenant-tile:{srv}:{r}", f"tenant:{srv}:{r}", "tenant-card"))
        nodes.append(text(tx, CARD_TOP + 24, f"GPU{r - 1}", size=11, fill=INK, weight="700"))
        nodes.append(text(tx, CARD_TOP + 37, f"rail {r}", size=8, fill=MUTED, weight="600"))
        nodes.append(f'<g id="cell-tenant-label:{srv}:{r}">'
                     + pill_html(tx, CARD_TOP + 55, size=8, word="tenant",
                                 extra=" font-weight: 700; padding: 0px 3px; border-radius: 6px;")
                     + "</g>")
        cells.append((f"tenant-label:{srv}:{r}", f"tenant:{srv}:{r}", "tenant-label"))
    nodes.append(place_icon(ICON_HOST, f"svg-image-{srv}", C_GPU, gx - 62, CARD_TOP + 124, 38))
    nodes.append(text(gx - 34, CARD_TOP + 112, srv, size=15, anchor="start"))
    nodes.append(text(gx - 34, CARD_TOP + 128, "4 × GB200, 1 NIC each", size=10, fill=MUTED, weight="500", anchor="start"))
    nodes.append(text(gx - 34, CARD_TOP + 142, "NVLink off · 1 VM per GPU", size=10, fill=MUTED, weight="500", anchor="start"))

for host, (hx, port, ch, role) in ISLAND.items():
    fill = C_HOST
    nodes.append(place_icon(ICON_HOST, f"svg-image-{host}", fill, hx, Y_HOST, S_HOST))
    nodes.append(text(hx, Y_HOST + S_HOST / 2 + 15, host, size=12))
    nodes.append(text(hx, Y_HOST + S_HOST / 2 + 29, role, size=9, fill=MUTED, weight="600"))

# Backend badge: overlay + operational state, live from the Backend CR (Export aifabric-backend)
BX, BY = W - 24, 22
bg.append(f'<rect x="{BX - 330}" y="{BY}" width="330" height="58" rx="10" fill="#f8fafc" stroke="#e2e8f0" stroke-width="1.5"/>')
nodes.append(text(BX - 314, BY + 22, "aifabrics Backend aifabric", size=12, fill=INK, weight="700", anchor="start"))
nodes.append(text(BX - 314, BY + 44, "overlay", size=11, fill=MUTED, weight="600", anchor="start"))
nodes.append(text(BX - 150, BY + 44, "state", size=11, fill=MUTED, weight="600", anchor="start"))
for cid, ref, x, w in (("backend-overlay", "backend:overlay", BX - 212, "overlay"),
                       ("backend-state", "backend:state", BX - 74, "state")):
    nodes.append(f'<g id="cell-{cid}">' + pill_html(x, BY + 40, size=11, word=w,
                 extra=" font-weight: 700; padding: 1px 8px; border-radius: 8px;") + "</g>")
    cells.append((cid, ref, cid))

# title + legend
deco = deco_top + [text(24, 34, "AI fabric GPU backend — live traffic", size=21, anchor="start", weight="700"),
        text(24, 55, "8 × 7220 IXR-H5-64D · SR Linux 26.7.1 · one aifabrics Backend on all 8 (EVPN, RoCEv2) · "
                     "one VRF per tenant · tenants book GPUs: tenant per GPU = label of its NIC's Interface in EDA (live)",
             size=12, fill=MUTED, weight="500", anchor="start")]
lx, ly = 24, H - 16
deco.append(text(lx, ly, "link:", size=12, fill="#475569", weight="700", anchor="start"))
xx = lx + 38


def legend_traffic(levels):
    global xx
    out = []
    for col, lab in levels:
        out.append(f'<rect x="{xx}" y="{ly - 9}" width="22" height="10" rx="2" fill="{col}" stroke="#475569" stroke-width="0.5"/>')
        out.append(text(xx + 27, ly, lab, size=11, fill="#475569", weight="500", anchor="start"))
        xx += 27 + len(lab) * 6.6 + 18
    return out


# ---- thresholds (per DIRECTION, bits/s), measured 2026-10-26 on this lab ------
# idle (LLDP/RA/BGP): server port ~1 kb/s, one ISL 0.2-0.5 kb/s, a 7-ISL bundle ~2 kb/s
# ping matrix (aifabric-traffic.py): GPU rail 15-37 kb/s, a flow-carrying ISL 1.3-17 kb/s,
#   bundle 4.5-29 kb/s; storage island (OpenShift control traffic) 1-300 kb/s
# (2026-10-26, before per-GPU tenancy) training step (k8s.sh allreduce, removed): 5-40 Mbit/s per rail;
#   its successor k8s.sh inference: see plan.md § PER-GPU TENANCY for measured rates
# => green = "carries traffic", yellow/orange = job traffic, red kept far above what the
#    simulator forwards (the SR Linux rate gauge spikes, telemetry-stack §21l)
UPPER = [("#FFFF00", 1000000), ("#FF8000", 10000000), ("#FF3154", 100000000)]
T_LINK = [("#bec8d2", 0), ("#4BDD33", 5000)] + UPPER
T_STRAND = [("#bec8d2", 0), ("#4BDD33", 1000)] + UPPER
T_BUNDLE = [("#bec8d2", 0), ("#4BDD33", 3000)] + UPPER
deco += legend_traffic([("#bec8d2", "idle"), ("#4BDD33", "traffic"), ("#FFFF00", "> 1 Mb/s"),
                        ("#FF8000", "> 10 Mb/s"), ("#FF3154", "> 100 Mb/s")])
xx += 10
deco.append(text(xx, ly, "port:", size=12, fill="#475569", weight="700", anchor="start"))
xx += 40
for col, lab in [("#4BDD33", "up"), ("#FF3154", "down")]:
    deco.append(f'<circle cx="{xx + 5}" cy="{ly - 4}" r="5" fill="{col}" stroke="#475569" stroke-width="1"/>')
    deco.append(text(xx + 15, ly, lab, size=11, fill="#475569", weight="500", anchor="start"))
    xx += 15 + len(lab) * 6.6 + 18
xx += 10
deco.append(f'<circle cx="{xx + 8}" cy="{ly - 4}" r="8" fill="#4BDD33" stroke="#475569" stroke-width="1"/>')
deco.append(text(xx + 8, ly - 1, "7", size=9, fill=INK, weight="700"))
deco.append(text(xx + 21, ly, "ISLs up in the bundle", size=11, fill="#475569", weight="500", anchor="start"))
xx += 21 + 21 * 6.6 + 18
deco.append(text(xx, ly, "GPUs:", size=12, fill="#475569", weight="700", anchor="start"))
xx += 44
for t in (1, 2, 0):                                  # live count per tenant (+ free) = cell gpus:<t>
    name = f"tenant-{t}" if t else "free"
    deco.append(f'<rect x="{xx}" y="{ly - 11}" width="64" height="14" rx="7" fill="{TENANT[t][0]}" stroke="{TENANT[t][1]}" stroke-width="1"/>')
    deco.append(text(xx + 32, ly, name, size=10, fill=TENANT[t][1], weight="700"))
    pills_svg.append(f'<g id="cell-gpus:{t}">' + pill_html(xx + 80, ly - 4, size=11, word="n",
                     extra=" font-weight: 700; padding: 0px 4px;") + "</g>")
    cells.append((f"gpus:{t}", f"gpus:{t}", "count-gpus"))
    xx += 104
deco.append(text(W - 24, ly, "arrow = direction · pill = live rate · strand = one 800G ISL", size=11,
                 fill="#94a3b8", weight="500", anchor="end"))
LEGEND_END = xx

SVG = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
       f'viewBox="0 0 {W} {H}" width="{W}" height="{H}">'
       f'<rect width="{W}" height="{H}" fill="#ffffff"/>'
       + "".join(bg) + "".join(strands_svg) + "".join(links_svg) + "".join(nodes)
       + "".join(overlay_svg) + "".join(pills_svg) + "".join(ports_svg) + "".join(deco) + "</svg>")


# ---- panelConfig ------------------------------------------------------------
def thr(name, levels):
    return [f"  {name}: &{name}"] + [f'    - {{ color: "{c}", level: {lv} }}' for c, lv in levels]


# Anchors MUST sit under `anchors:` or the plugin drops the whole config (telemetry-stack §22).
pc = ["---", "anchors:"]
pc += thr("thresholds-operstate", [("#FF3154", 0), ("#4BDD33", 1)])
pc += thr("thresholds-traffic", T_LINK)
pc += thr("thresholds-strand", T_STRAND)
pc += thr("thresholds-bundle", T_BUNDLE)
pc += thr("thresholds-bundle-up", [("#FF3154", 0), ("#FFFF00", 1), ("#4BDD33", ISL_PER_SPINE)])
pc += thr("thresholds-tenant-card", [(TENANT[t][0], t) for t in sorted(TENANT)])
pc += thr("thresholds-tenant-text", [(TENANT[t][1], t) for t in sorted(TENANT)])
pc += ["  label-config: &label-config", "    separator: replace", "    units: bps", "    decimalPoints: 0",
       "  count-config: &count-config", "    separator: replace", "    units: none", "    decimalPoints: 0",
       "  tenant-config: &tenant-config", "    separator: replace", "    valueMappings:",
       '      - { value: null, text: "?" }', '      - { value: 0, text: "free" }']
pc += [f'      - {{ value: {t}, text: "tenant-{t}" }}' for t in sorted(TENANT) if t]
pc += thr("thresholds-backend-state", [("#FF3154", 0), ("#16a34a", 1), ("#FF8000", 2), ("#64748b", 3)])
pc += thr("thresholds-backend-overlay", [("#64748b", 0), ("#1d4ed8", 1), ("#7c3aed", 2)])
pc += ["  overlay-config: &overlay-config", "    separator: replace", "    valueMappings:",
       '      - { value: null, text: "?" }', '      - { value: 1, text: "RouteLeaking" }',
       '      - { value: 2, text: "EVPN" }',
       "  state-config: &state-config", "    separator: replace", "    valueMappings:",
       '      - { value: null, text: "?" }', '      - { value: 0, text: "Down" }', '      - { value: 1, text: "Up" }',
       '      - { value: 2, text: "Degraded" }', '      - { value: 3, text: "Unknown" }']
pc += ["", "cellIdPreamble: cell-", "cells:"]
for cid, dref, kind in cells:
    pc += [f"  {cid}:", f"    dataRef: {dref}"]
    if kind == "traffic":
        pc += ["    fillColor:", "      thresholds: *thresholds-traffic",
               "    strokeColor:", "      thresholds: *thresholds-traffic"]
    elif kind == "rate":
        pc += ["    label: *label-config"]
    elif kind == "strand":
        pc += ["    strokeColor:", "      thresholds: *thresholds-strand"]
    elif kind == "bundle":
        pc += ["    label: *label-config", "    fillColor:", "      thresholds: *thresholds-bundle"]
    elif kind == "count":
        pc += ["    label: *count-config", "    fillColor:", "      thresholds: *thresholds-bundle-up"]
    elif kind == "count-gpus":
        pc += ["    label: *count-config"]
    elif kind == "count-dot":
        pc += ["    fillColor:", "      thresholds: *thresholds-bundle-up"]
    elif kind == "tenant-card":
        pc += ["    fillColor:", "      thresholds: *thresholds-tenant-card"]
    elif kind == "backend-overlay":
        pc += ["    label: *overlay-config", "    labelColor:", "      thresholds: *thresholds-backend-overlay"]
    elif kind == "backend-state":
        pc += ["    label: *state-config", "    labelColor:", "      thresholds: *thresholds-backend-state"]
    elif kind == "tenant-label":
        pc += ["    label: *tenant-config", "    labelColor:", "      thresholds: *thresholds-tenant-text"]
    else:
        pc += ["    fillColor:", "      thresholds: *thresholds-operstate"]
PANELCONFIG = "\n".join(pc) + "\n"


# ---- dashboard ----------------------------------------------------------------
def target(ref, expr, legend):
    # instant + last_over_time: the exporter omits a node from ~1 in 3 scrapes (telemetry-stack §23)
    return {"datasource": DS, "refId": ref, "expr": expr, "legendFormat": legend, "editorMode": "code",
            "instant": True, "range": False, "interval": "1s", "exemplar": False}


SEL = f'namespace_name="{NS}"'
ISL_RE = 'ethernet-1/(5[1-9]|6[0-4])'


def bundle_expr(metric):
    """leaf-side ISL ports grouped per spine: e1-51..57 -> l2-1, e1-58..64 -> l2-2."""
    inner = f'last_over_time({metric}{{{SEL},node_name=~"l1-[1-6]",interface_name=~"{ISL_RE}"}}[20s])'
    inner = f'label_replace({inner}, "spine", "l2-1", "interface_name", "ethernet-1/5[1-7]")'
    inner = f'label_replace({inner}, "spine", "l2-2", "interface_name", "ethernet-1/(5[89]|6[0-4])")'
    return f"sum by (node_name, spine) ({inner})"


RES = f'namespace_resources_cr_interfaces_eda_nokia_com_v1_interface{{{SEL},tenant="{{t}}"}}'


def tenant_expr():
    """value = tenant number (0 = free) per GPU, keyed <gpu_server>:<rail> (= its NIC's Interface)."""
    parts = []
    for t in sorted(TENANT):
        sel = RES.replace("{t}", str(t) if t else "")
        gpu = f'label_join(max by (gpu_server, rail) ({sel[:-1]},role="gpu"}}), "host", ":", "gpu_server", "rail")'
        parts.append(f"({gpu} * {t})")
    return " or ".join(parts)


def count_expr(t):
    """GPUs booked by tenant t (0 = free)."""
    return f'count({RES.replace("{t}", str(t) if t else "")[:-1]},role="gpu"}}) or vector(0)'


TARGETS = [
    target("A", f"last_over_time(node_srl_interface_oper_state{{{SEL}}}[20s])",
           "oper-state:{{node_name}}:{{interface_name}}"),
    target("B", f"last_over_time(node_srl_interface_traffic_rate_out_bps{{{SEL}}}[20s])",
           "{{node_name}}:{{interface_name}}:out"),
    target("C", f"last_over_time(node_srl_interface_traffic_rate_in_bps{{{SEL}}}[20s])",
           "{{node_name}}:{{interface_name}}:in"),
    target("D", bundle_expr("node_srl_interface_traffic_rate_out_bps"), "bundle:{{node_name}}:{{spine}}:out"),
    target("E", bundle_expr("node_srl_interface_traffic_rate_in_bps"), "bundle:{{node_name}}:{{spine}}:in"),
    target("F", bundle_expr("node_srl_interface_oper_state"), "bundle-up:{{node_name}}:{{spine}}"),
    target("G", tenant_expr(), "tenant:{{host}}"),
    target("J", count_expr(1), "gpus:1"),
    target("K", count_expr(2), "gpus:2"),
    target("L", count_expr(0), "gpus:0"),
    target("H", f'last_over_time(resources_cr_aifabrics_eda_nokia_com_v1_backend_spec_type_overlay{{{SEL},backend_name="aifabric"}}[60s])',
           "backend:overlay"),
    target("I", f'last_over_time(resources_cr_aifabrics_eda_nokia_com_v1_backend_status_operationalState{{{SEL},backend_name="aifabric"}}[60s])',
           "backend:state"),
]
flow = {
    "id": 1, "type": "andrewbmchugh-flow-panel", "title": "", "transparent": True,
    "datasource": DS, "gridPos": {"h": 26, "w": 24, "x": 0, "y": 0}, "interval": "1s",
    "targets": TARGETS,
    "fieldConfig": {"defaults": {}, "overrides": []},
    "options": {"svg": SVG, "panelConfig": PANELCONFIG, "siteConfig": "",
                "animationsEnabled": True, "animationControlEnabled": True, "highlighterEnabled": True,
                "highlighterSelection": "", "panZoomEnabled": False, "testDataEnabled": False,
                "timeSliderEnabled": False, "timeSliderMode": "local",
                "debuggingCtr": {"colorsCtr": 0, "dataCtr": 0, "displaySvgCtr": 0, "mappingsCtr": 0,
                                 "timingsCtr": 0}},
}
dash = {
    # no "id"/"version": Grafana keeps its DB copy over a file with an older version
    "uid": "aifabric-topology", "title": "AI fabric GPU backend",
    "description": "aifabric demo: live traffic on every GPU rail, every ISL and the storage island; tenant per GPU (per NIC)",
    "tags": ["aifabric"], "timezone": "browser", "editable": False,
    "refresh": "5s", "time": {"from": "now-5m", "to": "now"},
    "timepicker": {"refresh_intervals": ["5s", "10s", "30s"]},
    "schemaVersion": 41, "graphTooltip": 0, "panels": [flow], "templating": {"list": []},
    "annotations": {"list": []},
}


def check():
    """Evaluate every target in live Prometheus, build the legend keys, check every dataRef."""
    saved = Path(__file__).resolve().parent.parent / ".state" / "kubectl"
    kubectl = os.environ.get("KUBECTL") or (str(saved) if os.access(saved, os.X_OK) else "kubectl")
    keys = {}
    for t in TARGETS:
        q = subprocess.run([kubectl, "-n", NS, "exec", "deploy/prometheus", "--", "wget", "-qO-",
                            "--post-data", "query=" + t["expr"].replace("%", "%25").replace("&", "%26")
                            .replace("+", "%2B"),
                            "http://localhost:9090/api/v1/query"], capture_output=True, text=True)
        try:
            res = json.loads(q.stdout)["data"]["result"]
        except Exception:
            print(f"target {t['refId']}: query failed: {q.stdout[:200]} {q.stderr[:200]}")
            return 1
        for r in res:
            k = re.sub(r"\{\{(\w+)\}\}", lambda m: r["metric"].get(m.group(1), ""), t["legendFormat"])
            keys[k] = float(r["value"][1])
        print(f"target {t['refId']}: {len(res)} series")
    refs = sorted({d for _, d, _ in cells})
    miss = [d for d in refs if d not in keys]
    print(f"dataRefs resolved: {len(refs) - len(miss)}/{len(refs)}")
    for d in miss[:30]:
        print("  MISSING", d)
    for d in refs:
        if d.startswith(("tenant:", "gpus:", "bundle-up:", "backend:")):
            print(f"  {d} = {keys.get(d)}")
    return 1 if miss else 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    OUT.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n")
    n_links = len([c for c in cells if c[0].startswith("link_id:")]) // 2
    print(f"wrote {OUT.name}: {OUT.stat().st_size:,} B · svg {len(SVG):,} B · {n_links} server links · "
          f"{len([c for c in cells if c[2] == 'strand']) // 2} ISL strands · {len(cells)} cells")
