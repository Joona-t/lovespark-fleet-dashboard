# Bugs & Iterations — Fleet Dashboard

## 2026-11-12: v1.2.1, Lexend for the Korean themes

- **What:** the four Korean themes read in Lexend (SIL OFL, variable 300–700), served by the same exact-name font route.
- **Verify:** `test_only_the_theme_fonts_are_served` covers the three font files; 41 tests pass.

## 2026-11-12: v1.2.0, Glyph Cream and Glyph Basalt

- **What:** Cream and Basalt with Glyph Grid Studio's glyph art behind the page: a radial field of the studio's gradient character ramp on a canvas, drawn once per theme change or resize.
- **Verify:** drawn in 8–15 ms; per-update script time unchanged against the plain themes (BENCHMARKS.md); 41 tests pass.

## 2026-11-12: v1.1.0, Cream and Basalt themes

- **What:** two themes from LoveSpark's Glyph Grid Studio: Cream (aged paper, sage) and Basalt (dark stone, ember gold), with Cinzel and EB Garamond (SIL OFL, `fonts/OFL.txt`) served by an exact-name `/fonts/` route, lining figures and film grain.
- **Verify:** `test_only_the_two_theme_fonts_are_served` (two names serve as font/woff2; `..`, encoded paths and other names get the page); 41 tests pass. Page cost per theme measured in BENCHMARKS.md: no measurable difference.

## 2026-11-12: v1.0.0, first public release

- **What:** the dashboard we run on our own Macs, Linux and Windows machines, released with the machine list moved to `devices.json` (not in git; `devices.example.json` shows the shape). Without one it shows the machine it runs on.
- **Lessons carried in from the private build**, each with a test:
  - Agent output is untrusted: every sample is rebuilt against a schema (`clean()`), and the page escapes what it shows (`test_no_shape_of_agent_output_can_break_the_stream`, `test_hostile_agent_output_cannot_reach_the_page_as_markup`).
  - DNS rebinding: the server answers only IP addresses, `localhost` and its own names in `Host` (`test_host_header_allowlist_blocks_dns_rebinding`).
  - The Windows agent must not outlive its SSH session, including one that goes silent before the script arrives: the start command reads stdin with a 30 s deadline (`test_windows_agent_start_gives_up_on_a_silent_session`).
  - `nvidia-smi` launches were most of a Linux tick; NVML through `ctypes` reads the same fields 22x cheaper (BENCHMARKS.md).
- **Verify:** 40 tests pass on Python 3.9 and 3.15; running on four machines (two Macs, a Windows PC, a dual-boot Linux/Windows GPU box).
