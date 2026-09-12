"""Physical inventory remains separate from Spotify listener profiles."""

import json
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import server
import vinyl_library


URI = 'spotify:album:0nmLUAAUIUfm8q8Mq3mRAV'
ALBUM = {'title': 'Crosses', 'subtitle': 'Crosses', 'uri': URI,
         'image': 'https://i.scdn.co/image/example'}


def test_catalog_preserves_unavailable_items_and_hides_import_annotations(tmp_path):
    path = tmp_path / 'vinyls.json'
    raw = [dict(ALBUM, title='Fixture album', source_photo='fixture-cover.heic'),
           dict(ALBUM, uri='spotify:album:' + 'b' * 22, title='Unavailable fixture',
                availability='unavailable', availability_note='Currently unavailable')]
    path.write_text(json.dumps({'albums': raw}))
    items = vinyl_library.load_vinyls(path)
    assert len(items) == len(raw) == 2
    assert len({item['uri'] for item in items}) == len(items)
    assert items[1]['playable'] is False
    assert 'unavailable' in items[1]['availability_note']
    assert all('source_photo' not in item and 'verified_on' not in item for item in items)


@pytest.mark.parametrize('payload', [None, [], {'albums': {}}, {'albums': [None, 4]},
    {'albums': [dict(ALBUM, uri='spotify:playlist:' + 'a' * 22)]},
    {'albums': [dict(ALBUM, uri='spotify:album:../../secret')]},
    {'albums': [dict(ALBUM, title=['invalid'])]}])
def test_invalid_catalog_entries_are_not_playable(tmp_path, payload):
    path = tmp_path / 'vinyls.json'
    path.write_text(json.dumps(payload))
    assert vinyl_library.load_vinyls(path) == []


def test_catalog_load_is_bounded_and_observes_edits(tmp_path):
    path = tmp_path / 'vinyls.json'
    assert vinyl_library.load_vinyls(path) == []
    path.write_text('{')
    assert vinyl_library.load_vinyls(path) == []
    path.write_bytes(b'x' * (vinyl_library.MAX_CATALOG_BYTES + 1))
    assert vinyl_library.load_vinyls(path) == []
    path.write_text(json.dumps({'albums': [ALBUM, dict(ALBUM)]}))
    assert len(vinyl_library.load_vinyls(path)) == 1
    path.write_text(json.dumps({'albums': [dict(ALBUM, title='A new title', image='javascript:alert(1)')]}))
    item = vinyl_library.load_vinyls(path)[0]
    assert item['title'] == 'A new title' and item['image'] == ''


def test_vinyls_are_in_generic_and_linked_crates_without_remote_metadata(tmp_path, monkeypatch):
    path = tmp_path / 'vinyls.json'
    path.write_text(json.dumps({'albums': [ALBUM]}))
    monkeypatch.setattr(server, 'VINYLS_FILE', str(path))
    monkeypatch.setattr(server, 'load_idle_playlists', lambda: [])
    monkeypatch.setattr(server, '_profile_epoch_matches', lambda *_: True)
    for name in ('fetch_user_playlists', 'fetch_saved_albums', 'fetch_top_albums'):
        monkeypatch.setattr(server, name, lambda **_: [])
    monkeypatch.setattr(server, 'get_client_token', lambda: pytest.fail('catalog browsing must stay local'))
    for account in (None, 'listener-a', 'listener-b'):
        payload = server._build_crate_payload(account, 'epoch')
        section = next(section for section in payload['sections'] if section['id'] == 'vinyls')
        assert section['title'] == 'Vinyls'
        assert section['items'][0]['uri'] == URI


def test_add_vinyl_preserves_import_metadata_and_sanitizes_only_new_entry(tmp_path):
    path = tmp_path / 'vinyls.json'
    existing = dict(ALBUM, id='photo-record-7', source_photo='fixture-cover.heic',
                    annotations={'sleeve': ['gatefold', 'signed']}, edition_note='Original pressing')
    original = {'version': 1, 'title': 'My Vinyls', 'import': {'photos': ['fixture-cover.heic']},
                'albums': [existing, {'source_photo': 'unidentified.heic', 'notes': 'Identify later'}]}
    path.write_text(json.dumps(original))
    path.chmod(0o640)
    album = dict(ALBUM, uri='spotify:album:' + 'b' * 22, title='  New record  ',
                 subtitle='  Björk  ', spotify_title='New record', spotify_artists=['Björk'],
                 spotify_release_date='2026-09', spotify_total_tracks=9, verified_on='2026-09-12',
                 market='GB', edition_note='Blue vinyl', source_photo='not-an-import.jpg',
                 refresh_token='must-not-be-saved', id='client-controlled-id', arbitrary={'drop': True})

    item, added = vinyl_library.add_vinyl(path, album)

    stored = json.loads(path.read_text())
    assert added is True
    assert stored['albums'][:2] == original['albums']
    assert {key: value for key, value in stored.items() if key != 'albums'} == {
        key: value for key, value in original.items() if key != 'albums'}
    entry = stored['albums'][-1]
    assert entry['title'] == 'New record' and entry['subtitle'] == 'Björk'
    assert entry['id'] == 'vinyl-' + 'b' * 22
    assert entry['spotify_release_date'] == '2026-09' and entry['spotify_total_tracks'] == 9
    assert entry['market'] == 'GB' and entry['verified_on'] == '2026-09-12'
    assert all(key not in entry for key in ('source_photo', 'refresh_token', 'arbitrary'))
    assert item == vinyl_library.load_vinyls(path)[-1]
    assert path.stat().st_mode & 0o777 == 0o640


def test_add_vinyl_initializes_missing_catalog_and_accepts_missing_art(tmp_path):
    path = tmp_path / 'vinyls.json'
    item, added = vinyl_library.add_vinyl(path, dict(ALBUM, image=''))
    assert added is True and item['image'] == ''
    assert json.loads(path.read_text())['title'] == 'Vinyls'
    assert vinyl_library.load_vinyls(path) == [item]


def test_duplicate_addition_returns_stored_edition_without_rewriting(tmp_path, monkeypatch):
    path = tmp_path / 'vinyls.json'
    existing = dict(ALBUM, title='Original pressing', source_photo='photo.heic',
                    availability='unavailable', availability_note='Not available in this market')
    raw = json.dumps({'albums': [existing], 'annotations': {'keep': True}}, separators=(',', ':')).encode()
    path.write_bytes(raw)
    monkeypatch.setattr(vinyl_library.os, 'replace', lambda *_: pytest.fail('duplicates must not rewrite the catalog'))

    item, added = vinyl_library.add_vinyl(path, dict(ALBUM, title='Replacement title'))

    assert added is False and item['title'] == 'Original pressing'
    assert item['playable'] is False
    assert path.read_bytes() == raw


@pytest.mark.parametrize('raw', [b'{', b'[]', b'{}', b'{"albums":{}}',
    b'{"albums":[null]}', b'{"albums":[],"albums":[]}',
    b'{"albums":[],"notes":NaN}', b'{"albums":[],"notes":"\xff"}'])
def test_add_vinyl_refuses_malformed_catalog_without_overwriting(tmp_path, raw):
    path = tmp_path / 'vinyls.json'
    path.write_bytes(raw)
    with pytest.raises(vinyl_library.VinylCatalogError):
        vinyl_library.add_vinyl(path, ALBUM)
    assert path.read_bytes() == raw


@pytest.mark.parametrize('change', [
    {'uri': 'spotify:album:' + 'a' * 21}, {'uri': 'spotify:album:' + 'a' * 23},
    {'uri': 'spotify:playlist:' + 'a' * 22}, {'uri': 'spotify:album:../../secret'},
    {'title': None}, {'title': '   '}, {'title': 'x' * 501}, {'title': 'bad\x00title'},
    {'subtitle': []}, {'subtitle': '  '}, {'image': None}, {'image': 'javascript:alert(1)'},
    {'image': 'https://i.scdn.co.evil.example/image/cover'},
    {'image': '/static/../config.json'}, {'image': '/static/%2e%2e/config.json'},
    {'image': 'https://i.scdn.co/image/cover?secret=1'},
    {'spotify_release_date': '2026-02-30'}, {'spotify_release_date': '2026-13'},
    {'verified_on': '2026-09'}, {'spotify_total_tracks': True}, {'spotify_total_tracks': 0},
    {'spotify_artists': []}, {'spotify_artists': ['']}, {'market': 'gb'},
    {'availability': 'maybe'},
])
def test_add_vinyl_validates_new_album_without_changing_catalog(tmp_path, change):
    path = tmp_path / 'vinyls.json'
    raw = b'{"albums":[],"source_photo":"keep.heic"}'
    path.write_bytes(raw)
    with pytest.raises(vinyl_library.VinylValidationError):
        vinyl_library.add_vinyl(path, dict(ALBUM, **change))
    assert path.read_bytes() == raw


def test_add_vinyl_enforces_record_limit_but_remains_idempotent_when_full(tmp_path):
    path = tmp_path / 'vinyls.json'
    albums = [ALBUM] + [dict(ALBUM, uri=f'spotify:album:{index:022d}')
                        for index in range(vinyl_library.MAX_VINYLS - 1)]
    raw = json.dumps({'albums': albums}).encode()
    path.write_bytes(raw)
    assert vinyl_library.add_vinyl(path, ALBUM)[1] is False
    with pytest.raises(vinyl_library.VinylCatalogError, match='full'):
        vinyl_library.add_vinyl(path, dict(ALBUM, uri='spotify:album:' + 'z' * 22))
    assert path.read_bytes() == raw


def test_add_vinyl_refuses_oversized_catalog_and_growth_past_byte_limit(tmp_path):
    path = tmp_path / 'vinyls.json'
    raw = b'x' * (vinyl_library.MAX_CATALOG_BYTES + 1)
    path.write_bytes(raw)
    with pytest.raises(vinyl_library.VinylCatalogError, match='size limit'):
        vinyl_library.add_vinyl(path, ALBUM)
    assert path.read_bytes() == raw
    base = {'albums': [], 'photo_annotation': ''}
    base['photo_annotation'] = 'x' * (vinyl_library.MAX_CATALOG_BYTES - len(json.dumps(base).encode()) - 10)
    raw = json.dumps(base).encode()
    assert len(raw) <= vinyl_library.MAX_CATALOG_BYTES
    path.write_bytes(raw)
    with pytest.raises(vinyl_library.VinylCatalogError, match='would exceed'):
        vinyl_library.add_vinyl(path, ALBUM)
    assert path.read_bytes() == raw


def test_failed_atomic_replace_keeps_original_catalog_and_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / 'vinyls.json'
    raw = b'{"albums": [], "photo_annotation": "Keep me"}'
    path.write_bytes(raw)

    def fail_replace(source, destination):
        assert Path(destination).read_bytes() == raw
        assert json.loads(Path(source).read_text())['albums'][0]['uri'] == URI
        raise OSError('simulated replace failure')

    monkeypatch.setattr(vinyl_library.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='simulated'):
        vinyl_library.add_vinyl(path, ALBUM)
    assert path.read_bytes() == raw
    assert not list(tmp_path.glob('.vinyls-*.tmp'))


def test_concurrent_threads_keep_every_record_and_deduplicate_retries(tmp_path):
    path = tmp_path / 'vinyls.json'
    path.write_text(json.dumps({'albums': [], 'annotation': {'photo': 'keep.heic'}}))
    barrier = threading.Barrier(8)

    def add(index):
        barrier.wait(timeout=10)
        return vinyl_library.add_vinyl(path, dict(ALBUM, uri=f'spotify:album:{index % 4:022d}'))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(add, range(16)))

    stored = json.loads(path.read_text())
    assert sum(added for _, added in results) == 4
    assert len(stored['albums']) == 4
    assert {item['uri'] for item in stored['albums']} == {f'spotify:album:{index:022d}' for index in range(4)}
    assert stored['annotation'] == {'photo': 'keep.heic'}


def test_concurrent_processes_do_not_lose_catalog_additions(tmp_path):
    path = tmp_path / 'vinyls.json'
    path.write_text(json.dumps({'albums': [], 'annotation': {'photo': 'keep.heic'}}))
    script = '''import json,sys
from vinyl_library import add_vinyl
sys.stdin.read(1)
item,added=add_vinyl(sys.argv[1],json.loads(sys.argv[2]))
assert added
'''
    root = Path(__file__).resolve().parents[1]
    processes = [subprocess.Popen([sys.executable, '-c', script, str(path),
                  json.dumps(dict(ALBUM, uri=f'spotify:album:{index:022d}'))],
                  cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for index in range(6)]
    try:
        for process in processes:
            process.stdin.write(b'1')
            process.stdin.flush()
        for process in processes:
            _, error = process.communicate(timeout=15)
            assert process.returncode == 0, error.decode()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    stored = json.loads(path.read_text())
    assert {item['uri'] for item in stored['albums']} == {f'spotify:album:{index:022d}' for index in range(6)}
    assert stored['annotation'] == {'photo': 'keep.heic'}
