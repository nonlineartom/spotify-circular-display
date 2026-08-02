# Live Pi release record — 2026-08-02

## Outcome

The 1 GB panel efficiency pass (`b3cbffc`, reviewed and validated on the host:
220 tests, full `scripts/validate.sh`, and a local kiosk smoke test) is live on
`pi5.local` as a routine fast-forward of the accepted release directory. All
services restarted cleanly during an idle window; no errors or warnings in any
journal after restart.

| Item | Released value |
|---|---|
| Branch | `premium-rework` (pushed to GitHub) |
| Code commit | `b3cbffc09a099e8471ddadebc89304083d026588` |
| Docs commits | `0d2b7e9` (procedures), this record |
| Pi release directory | `/home/admin/spotify-circular-display-auth-touch-413dfff` (detached, pinned) |
| Backup | `/home/admin/spotify-display-backups/routine-20260802-213424` (config.json, go-librespot config, pre-deploy SHA) |
| Live receiver config | `go-librespot/config.yml` local modification preserved across checkout |

## Verified live after restart

- Chromium runs with `--max-old-space-size=192`, `--enable-gpu-rasterization`
  and `--disable-background-networking`.
- `oom_score_adj` confirmed on the running processes: go-librespot **−500**,
  WLED renderer **+500**.
- `/api/health` healthy; `/api/events` serves the playback event immediately;
  receiver reports its session (`Pi Display`, idle at deploy time).
- Memory directly after restart: 548 MB used / 441 MB available / 333 MB swap.

## Deviation — MemoryHigh dormant until a host change

The Pi firmware adds `cgroup_disable=memory` on ≤1 GB boards, so the unified
cgroup exposes no memory controller (`/sys/fs/cgroup/cgroup.controllers` lists
`cpuset cpu io pids` only). systemd accepts the new
`MemoryHigh=350M/600M` directives but they cannot act. To activate them, add
`cgroup_enable=memory` to `/boot/firmware/cmdline.txt` (single line) and
reboot; the controller costs a small amount of kernel page metadata on this
1 GB board, which is why the firmware disables it by default. The OOM-kill
ordering (receiver protected, WLED sacrificed first) works regardless and is
live now.

## Notes

- Unrelated pre-existing warning during unit verification:
  `/etc/systemd/system/spotify-voice-bridge.service` (not part of this
  repository) is marked executable; `sudo chmod 644` silences it.
- Watch items from the review: kiosk RSS relative to the (currently dormant)
  600 MB threshold, any GPU-rasterization artifacts on the panel, and non-latin
  track titles now rendering in fallback glyphs (latin-only Montserrat).
