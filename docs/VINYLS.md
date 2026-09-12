# Vinyls collection

Open **Explore music → Vinyls** to browse your physical records. Use **+ Add**
and the on-screen keyboard to find a Spotify edition and save it to this Pi.
Album details also provide **+ Vinyls**, **Play album** and **Queue album**.

The personal catalogue is stored in `data/vinyls.json` by default and is ignored
by Git. For a new installation, copy `data/vinyls.example.json` to that path, or
add the first record through the display. Set `SPOTIFY_DISPLAY_VINYLS` to use a
persistent location outside the release directory. Preserve that file across
updates and rollbacks; never replace it with an example catalogue.

Entries contain a title, artist (`subtitle`), Spotify album URI and optional
cover URL. Edition notes and unavailable flags are supported. Imported photo
annotations stay local and are omitted from browser responses. Duplicate
albums are not added twice. Saves use locking and atomic replacement.

The repository ships no personal record inventory or source photos. Tests use
synthetic inventories. The mock preview uses a temporary catalogue and fake
playback; `MOCK_VINYLS_CATALOG=1` optionally copies a local private catalogue
into that temporary directory.
