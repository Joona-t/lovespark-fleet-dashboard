"""Build the Glyph themes' title art with Glyph Grid Studio's charcoal-sketch skill (the look of LoveSpark's Forge test bench).

usage: python3 tools/build_charcoal.py <charcoal-sketch render.mjs> [work dir]      (needs numpy and Pillow; the
       renderer must know the basalt palette, e.g. glyph-grid-studio skills/charcoal-sketch from 2026-10-18 on)

Same recipe as lovespark.love's basalt titles (ggs-int scripts/site-titles):
Cinzel 400 at -0.02em, cream ink #E6D6AD on basalt #16171A, screenshot at 2x in
headless Chrome, rendered with `--palette basalt --dust edge --cell 3`, keyed to
alpha. Writes to assets/:
  <name>.webp  alpha still (reduced motion, and the video's poster)
  <name>.mp4   the skill's 4 s breathing loop at 12 fps, premultiplied onto black,
               shown with mix-blend-mode: screen (H.264 carries no alpha)
  <name>-ink.webp  the same still in dark ink, for Glyph Cream (paper)
"""
import glob, os, shutil, subprocess, sys, tempfile
import numpy as np
from PIL import Image

RENDER = sys.argv[1]
WORK = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix='forge-charcoal-')
HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.dirname(HERE)
OUT = os.path.join(DASH, 'assets')
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
BG, INK = '#16171A', '#E6D6AD'
DURATION, FPS = 4, 12
# name -> (text, font size px at 1x)
ART = {'fleet-title': ('Fleet', 96), 'fleet-star': ('✺', 120)}
hexv = lambda h: np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], float)
bg, ink = hexv(BG), hexv(INK)


def smooth(e0, e1, x):
    k = np.clip((x - e0) / (e1 - e0), 0, 1)
    return k * k * (3 - 2 * k)


def alpha(rgb):
    return np.clip(np.linalg.norm(rgb - bg, axis=2) / np.linalg.norm(ink - bg) * 1.12, 0, 1)


def source(name, text, size):
    html = os.path.join(WORK, name + '.html')
    font = 'file://' + os.path.join(DASH, 'fonts', 'cinzel.woff2')
    open(html, 'w').write(
        f'<!doctype html><meta charset="utf-8"><style>@font-face{{font-family:C;src:url("{font}")}}'
        f'html,body{{margin:0;background:{BG}}}div{{font:400 {size}px C,Georgia,serif;letter-spacing:-.02em;'
        f'color:{INK};padding:{size}px;white-space:nowrap;display:inline-block}}</style><div>{text}</div>')
    shot = os.path.join(WORK, name + '-shot.png')
    subprocess.run([CHROME, '--headless=new', '--hide-scrollbars', '--virtual-time-budget=3000',
                    '--allow-file-access-from-files', '--force-device-scale-factor=2', '--window-size=1400,600',
                    '--screenshot=' + shot, 'file://' + html], check=True, capture_output=True)
    im = Image.open(shot).convert('RGB')
    ys, xs = np.nonzero(alpha(np.asarray(im).astype(float)) > 0.1)
    fs = size * 2
    box = (xs.min() - 0.30 * fs, ys.min() - 0.40 * fs, xs.max() + 0.30 * fs, ys.max() + 0.30 * fs)
    crop = im.crop(tuple(round(v) for v in box))
    w, h = crop.size
    crop = crop.crop((0, 0, w - w % 2, h - h % 2))
    src = os.path.join(WORK, name + '.png')
    crop.save(src)
    return src


def still(name, src):
    d = os.path.join(WORK, 'still-' + name)
    subprocess.run(['node', RENDER, src, '--palette', 'basalt', '--formats', 'still', '--dust', 'edge',
                    '--cell', '3', '--maxdim', str(max(Image.open(src).size)), '--out', d],
                   check=True, capture_output=True)
    a = alpha(np.asarray(Image.open(os.path.join(d, os.path.basename(src))).convert('RGB')).astype(float))
    rgba = np.zeros(a.shape + (4,), np.uint8)
    rgba[..., :3] = ink.astype(np.uint8)
    rgba[..., 3] = (a * 255).round().astype(np.uint8)
    Image.fromarray(rgba, 'RGBA').save(os.path.join(OUT, name + '.webp'), 'WEBP', quality=86, method=6)
    rgba[..., :3] = np.array([42, 36, 24], np.uint8)          # Cream's ink #2a2418, for the paper theme
    rgba[..., 3] = (a * smooth(0.06, 0.22, a) * 255).round().astype(np.uint8)   # no faint haze box on paper
    Image.fromarray(rgba, 'RGBA').save(os.path.join(OUT, name + '-ink.webp'), 'WEBP', quality=86, method=6)


def loop(name, src):
    d = os.path.join(WORK, 'anim-' + name)
    subprocess.run(['node', RENDER, src, '--palette', 'basalt', '--formats', 'gif', '--dust', 'edge', '--cell', '3',
                    '--maxdim', str(max(Image.open(src).size)), '--duration', str(DURATION), '--gif-fps', str(FPS),
                    '--gif-width', '160', '--gif-colors', '4', '--keep-frames', '--out', d],
                   check=True, capture_output=True)
    frames = sorted(glob.glob(os.path.join(d, '.work', 'frames', 'frame_*.png')))[:DURATION * FPS]
    comp = os.path.join(d, 'comp'); os.makedirs(comp, exist_ok=True)
    for i, f in enumerate(frames):
        al = alpha(np.asarray(Image.open(f).convert('RGB')).astype(float))
        al = al * smooth(0.05, 0.22, al)
        h, w = al.shape
        fy, fx = max(8, int(h * 0.08)), max(8, int(w * 0.04))
        ey = smooth(0, 1, np.minimum(np.arange(h), np.arange(h)[::-1]) / fy)
        ex = smooth(0, 1, np.minimum(np.arange(w), np.arange(w)[::-1]) / fx)
        rgb = ink * (al * ey[:, None] * ex[None, :])[..., None]
        Image.fromarray(rgb[:h - h % 2, :w - w % 2].round().astype(np.uint8), 'RGB').save(
            os.path.join(comp, f'c_{i:03d}.png'))
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-framerate', str(FPS), '-i', os.path.join(comp, 'c_%03d.png'),
                    '-c:v', 'libx264', '-preset', 'slow', '-crf', '32', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                    '-an', os.path.join(OUT, name + '.mp4')], check=True)
    shutil.rmtree(d)
    return len(frames)


os.makedirs(OUT, exist_ok=True)
for name, (text, size) in ART.items():
    src = source(name, text, size)
    still(name, src)
    n = loop(name, src)
    print(name, Image.open(src).size, n, 'frames',
          os.path.getsize(os.path.join(OUT, name + '.webp')) // 1024, 'KB webp',
          os.path.getsize(os.path.join(OUT, name + '.mp4')) // 1024, 'KB mp4', flush=True)
print('done, work dir', WORK)
