# Artwork, record details and collection additions

Selecting an album in Mosaic or Grid expands that exact cover into a 360px
sleeve. The tracks emerge from its right edge, revealing from left to right.
Back returns the artwork to its original cover and preserves the grid's scroll
position or the mosaic's pan. Closing, changing collections or switching
accounts cancels unfinished motion; reduced-motion preferences skip the flight.

Playing an album or an individual track carries the same artwork directly
onto the playback platter while the track drawer retracts. The detail view
stays in place until the handoff finishes; it never returns through Mosaic.
Queue and failed playback requests stay in the album view.

Record details use Spotify catalogue facts: release date at the precision
Spotify supplies, track count, a complete-album running time when available,
and label when supplied. No biography or release date is invented. Tracks and
facts load concurrently through a bounded shared worker pool; optional facts
cannot hold up ready tracks for more than one second.

## Add a physical record

Use **Explore music → + Add**. The on-screen keyboard supports searching by
album and artist; a Spotify album link also works. Choose an edition and tap
**Add to Vinyls**. Albums already visible elsewhere in Explore also have a
**+ Vinyls** action in their detail view. This updates the Pi's shared physical
collection. Existing records and imported photo annotations are preserved, and
adding the same Spotify album twice is harmless.

The server verifies catalogue facts again before saving. The endpoint requires
owner access and the current receiver profile epoch. Saves use an atomic file
replacement and a process/thread lock. A damaged or oversized catalogue is
reported without overwriting it. Capacity is 250 entries and 512 KiB.

On the Pi, `SPOTIFY_DISPLAY_VINYLS` should point to
`/home/pi/spotify-display-data/vinyls.json`. The service needs write access
to the directory for atomic replacement and its sidecar lock. Seed it only
once from the active catalogue, then keep it across releases and rollbacks.
For source checkouts, the default remains `data/vinyls.json`.

## Weekly playlists and mixes

Open **Explore music → Made for you**, or **Controls → Made for you**.
Discover Weekly and Release Radar are pinned above up to six Spotify mixes.
Only matching Spotify-owned playlists returned by the live account's library
are used; account changes clear previous results and pending requests.

Spotify's [current-user playlists endpoint](https://developer.spotify.com/documentation/web-api/reference/get-a-list-of-current-users-playlists)
returns owned/followed playlists. Save the weekly playlists and desired mixes
in Spotify, then use **Refresh playlists** if they do not appear. Some personal
mixes may remain unavailable through the API. The UI reports missing playlists
and never substitutes another account's or a generic playlist's ID.

## Validation without hardware

Run `scripts/validate.sh` with the project's Python environment. For a safe
preview, run `MOCK_DISPLAY_PORT=5108 python scripts/run_mock_display.py`.
Each mock process uses its own temporary catalogue. Search **blue hour** for
the addition fixture; the real catalogue and live Spotify/WLED are untouched.
