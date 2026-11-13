"""Layout check for the dashboard, run before every release: every theme and every font in the page's own Theme & font
menu, at 1440, 768 and 390 px, in the grid and with a machine's full stats open. Exit 1 on any problem.

Fails on: text on text (each line measured by its font's em box, so OpenDyslexic's tall line boxes don't count),
text running out of its card or the header (text clipped on purpose with an ellipsis counts only where it shows),
sideways scroll, the open menu leaving the screen, a Glyph theme whose glyph field draws nothing, and the glyph
field showing in any other theme.

    python3 tools/check_layout.py http://127.0.0.1:8766/        # a running dashboard; needs Playwright
"""
import sys
from playwright.sync_api import sync_playwright
url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766/"
JS = r"""() => {
  const out = [], W = innerWidth;
  if (document.documentElement.scrollWidth > W + 1) out.push(`page scrolls sideways (${document.documentElement.scrollWidth} in ${W})`);
  const leaves = [...document.querySelectorAll("main *")].filter(e => e.offsetParent && [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()));
  // clipped on purpose: an ellipsis, or an ancestor (inside the card) that hides overflow
  const clipped = e => { for (let a = e; a && !a.matches(".card, header, footer, main"); a = a.parentElement) { const cs = getComputedStyle(a);
    if (cs.textOverflow === "ellipsis" || cs.overflowX === "hidden" || cs.overflowX === "clip") return a.getBoundingClientRect(); } return null; };
  // one box per line the text occupies (a wrapped span is two boxes, not their union)
  const boxes = leaves.flatMap(e => { const r = document.createRange(); r.selectNodeContents(e);
      // measured by the font's em box around the line's middle, not its line box: OpenDyslexic's line boxes are
      // ~1.7 em tall, so stacked label and value boxes overlap while the letters are well apart
      const fs = parseFloat(getComputedStyle(e).fontSize);
      return [...r.getClientRects()].filter(q => q.width > 0 && q.height > 0).map(q => { const m = (q.top + q.bottom) / 2;
        const c = clipped(e);   // clipped text counts only where it is visible
        return {e, r: {left: c ? Math.max(q.left, c.left) : q.left, right: c ? Math.min(q.right, c.right) : q.right, top: m - fs * .55, bottom: m + fs * .55},
                card: e.closest(".card, header, footer"), clip: !!c}; }); });
  const name = e => (e.id ? "#" + e.id : e.tagName.toLowerCase() + (e.className && typeof e.className === "string" ? "." + e.className.split(" ")[0] : "")) + " " + JSON.stringify(e.textContent.trim().slice(0, 28));
  boxes.forEach(b => { if (!b.card) return; const c = b.card.getBoundingClientRect(), cs = getComputedStyle(b.e);
    if (b.clip) return;
    if (b.r.right > c.right + 1 || b.r.left < c.left - 1) out.push(`text runs out of its ${b.card.tagName === "HEADER" ? "header" : "card"}: ${name(b.e)}`); });
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j]; if (a.e === b.e || a.e.contains(b.e) || b.e.contains(a.e)) continue;
    const w = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left), h = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
    if (w > 2 && h > 3) out.push(`text on text: ${name(a.e)} / ${name(b.e)}`);
  }
  return out;
}"""
problems, combos = {}, 0
with sync_playwright() as p:
    b = p.chromium.launch()
    for vw in (1440, 768, 390):
        pg = b.new_page(viewport={"width": vw, "height": 1000})
        pg.goto(url, wait_until="domcontentloaded"); pg.wait_for_selector("#devices [data-id]", timeout=30000); pg.wait_for_timeout(1500)
        themes = pg.eval_on_selector_all("#theme-menu button[data-theme]", "bs => bs.map(b => b.dataset.theme)")
        fonts = pg.eval_on_selector_all("#theme-menu button[data-font]", "bs => bs.map(b => b.dataset.font)")
        for th in themes:
            pg.evaluate("t => setTheme(t)", th)
            for fo in fonts:
                pg.evaluate("f => setFont(f)", fo); pg.evaluate("document.fonts.ready"); pg.wait_for_timeout(150)
                where = f"{vw}px/{th or 'paper'}/{fo or 'own'}"
                for view in ("main", "detail"):
                    if view == "detail": pg.evaluate("() => { if (!S.open) openDev(S.devices[0].id); }"); pg.wait_for_timeout(150)
                    else: pg.evaluate("() => { if (S.open) openDev(S.open); }")
                    combos += 1
                    for m in pg.evaluate(JS): problems.setdefault(f"{view}: {m}", []).append(where)
                pg.evaluate("() => { if (S.open) openDev(S.open); }")
            # the menu, open, must fit the screen
            pg.click("#theme-btn"); r = pg.eval_on_selector("#theme-menu", "e => { const r = e.getBoundingClientRect(); return [r.left, r.right, innerWidth]; }"); pg.click("#theme-btn")
            if r[0] < -1 or r[1] > r[2] + 1: problems.setdefault(f"menu leaves the screen ({r[0]:.0f}..{r[1]:.0f} of {r[2]})", []).append(f"{vw}px/{th}")
            if th.startswith("glyph-"):
                pg.wait_for_timeout(1500)
                ink = pg.evaluate("""() => { const c = document.getElementById('glyphField'); if (!c || getComputedStyle(c).display === 'none') return -1;
                    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; }""")
                if ink <= 0: problems.setdefault("glyph field not drawn", []).append(f"{vw}px/{th}")
            else:
                if pg.evaluate("() => { const c = document.getElementById('glyphField'); return c && getComputedStyle(c).display !== 'none'; }"):
                    problems.setdefault("glyph field showing outside the Glyph themes", []).append(f"{vw}px/{th}")
        pg.close()
    b.close()
for m, w in problems.items(): print(f"FAIL {m}\n     in {len(w)}, e.g. {', '.join(w[:3])}")
print(f"{'FAIL' if problems else 'OK'}: {len(problems)} distinct problem(s) over {combos} theme x font x width x view combinations")
sys.exit(1 if problems else 0)
