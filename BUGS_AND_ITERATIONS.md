# Bugs & Iterations — Fleet Dashboard

## 2026-11-12: v1.3.0, Glyph themes with stardust, embers and glyph-art titles

- **What:** Glyph Cream and Glyph Basalt now have stardust, twinkling sparkles, embers and a glow (Basalt only, stopped in every other theme), and the title as charcoal glyph art built with Glyph Grid Studio's renderer (`tools/build_charcoal.py`): a looping video in Basalt, dark-ink stills on Cream, loaded only when a Glyph theme is chosen. An exact-name `/assets/` route serves them, with byte ranges for Safari.
- **Verify:** `test_title_art_is_served_by_exact_name_with_byte_ranges`; 42 tests pass. Ongoing cost per theme in BENCHMARKS.md.

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

---

# Before the public release (private build, 2026-11-11 to 2026-11-12)

The dashboard ran on our own four machines for a day and a half before this repo existed. These are the bugs found
and fixed in that time, each with what caused it, why it was missed, and the check that now stops it.

## BUG-005: Windows agents stuck forever on a silent session

- **What:** seven fleet agents were running on a Windows PC instead of one. Six had used no CPU at all, and most had lost their SSH session.
- **Root cause:** the start command was `[Console]::In.ReadToEnd() | Invoke-Expression`. When a session goes silent before the script has fully arrived (the far end asleep, off the network, or killed mid-send), that read never returns, so the agent never loads its own "exit when my session is gone" check (BUG-001).
- **Why it was missed:** BUG-001's fix lives inside the script, and its check dropped the client after the script had arrived, never during.
- **Fix:** the start command reads the raw stdin stream with a 30 s deadline (`Stream.ReadAsync` runs on a pool thread in .NET Framework, so `Wait()` can time out) and exits when it passes.
- **Verify:** a client frozen mid-script (SIGSTOP) left the old command stuck, and the new command was gone within 45 s; a normal start still gave samples and exit 0. `test_windows_agent_start_gives_up_on_a_silent_session` fails on the old command and passes on the new.
- **Enforcement:** partial: the test is static; the live check needs a Windows host.

## BUG-004: A malformed sample could stop the stream or kill a machine's reader

- **What:** the first fix for BUG-003 removed stray text but kept any dict or list. A sample with a dict where a number belongs (`"temp": {}`) crashed the alerts on every page update; a sample that was a list killed that machine's reader thread until a restart. Found by an automated security review.
- **Root cause:** the same class as BUG-003: agent output was trusted to have the right shape. BUG-003's fix treated one symptom (text) instead of the class.
- **Why it was missed:** the BUG-003 test put one text payload in each field and never tried other types.
- **Fix, at the level of the class:** `clean()` rebuilds every sample against a schema; a value survives only as the type its position allows, numbers must be finite and within ±1e15, everything else becomes `null`. `Fleet.ingest` is the only way agent data enters. Second line: each machine's alerts, reader and card are isolated, so one bad sample shows one error card.
- **Verify:** `test_no_shape_of_agent_output_can_break_the_stream` puts 17 hostile values at every position of a full sample; it failed on the BUG-003 fix and passes now. Real output from the Mac and Windows agents passes through `clean()` unchanged.
- **Caught on the way:** the first schema dropped each GPU's `power` reading, because `power` is also a top-level field; an existing test caught it and the schema became depth-aware.

## BUG-003: Agent output could inject markup into the page

- **What:** the page built its HTML from agent samples, and a few fields were interpolated without escaping. A compromised machine could send markup in them and run script in the dashboard. Found by an automated security review of the first build.
- **Root cause:** agent output was treated as trusted, and escaping was applied field by field, so any field assumed to be a number was a hole.
- **Fix:** `clean()` runs on every sample first (text only in named text fields, capped at 200 characters), and the page escapes everything it shows.
- **Verify:** one payload in every field of a sample, rendered in a browser: the first build's page created 24 injected elements and ran the handler; the fixed page created 0. `test_hostile_agent_output_cannot_reach_the_page_as_markup`.

## BUG-002: Any web page could read the dashboard's data through DNS rebinding

- **What:** the server answered every request whatever its `Host` header, so a web page open in a browser on the same machine could rebind its own domain to 127.0.0.1 and read `/api/state` (host names, process lists, SSH errors). Found by the same review.
- **Root cause:** binding to 127.0.0.1 was treated as enough. It stops other machines, not a browser on this one.
- **Fix:** the server answers only an IP address, `localhost` or its own names in `Host`, and 403 otherwise; `--allow-host` adds a name.
- **Verify:** IP addresses and `localhost` get 200; `evil.example` and `localhost.evil.example` get 403 on every route. `test_host_header_allowlist_blocks_dns_rebinding`.

## BUG-001: Windows agent kept running after its SSH session dropped

- **What:** after the server stopped or a connection was cut, the PowerShell agent stayed alive until its 10-minute lifetime cap.
- **Root cause:** the agent treated a failed write as the sign that the session was gone, but .NET's console stream swallows broken-pipe errors; Windows OpenSSH also leaves `cmd.exe` and its child running when the session ends.
- **Why it was missed:** Unix pipe behaviour was assumed on Windows, and the first check looked only at the agent's output, not at what was left behind.
- **Fix:** the agent finds its sshd session process at start and exits once that process is gone; the 10-minute cap stays as a second bound.
- **Verify:** dropping the SSH client left no agent at the next check, 10 s later.

## Performance, measured (see BENCHMARKS.md)

- Streaming one agent per machine over SSH measured as cheap as the best polling design and the freshest (56 / 57 / 68 ms of CPU per tick for stream / reused-connection polling / new-connection polling).
- Two `nvidia-smi` launches were 80% of a Linux tick; NVML through `ctypes` reads the same 42 fields in 2 ms instead of 45. With the process table read every third tick, a Linux tick went from 56 to 16 ms.
- The Mac agent dropped from ~100 to ~61 ms a tick (slow sensors every third tick), and the Windows agent's WMI cost by about a third.
