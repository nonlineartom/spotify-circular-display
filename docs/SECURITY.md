# Security and trust model

Spotify Circular Display is a home-LAN appliance, not an Internet-facing
Spotify proxy. Network locality reduces exposure, but it is not authorization:
guest, IoT and compromised browser devices can share the same LAN.

## Route boundary

The intentionally low-friction surface is limited to what the shared display
needs for ordinary playback:

- public now-playing/health state;
- metadata and track selection scoped to the album currently on the platter;
- Spotify Connect discovery handled by go-librespot;
- the supported local playback actions and configured public idle choices.

Owner authorization is required for:

- private playlists, saved albums and retained listening history;
- OAuth link creation, status and disconnect;
- WLED discovery, status and persistent configuration;
- detailed diagnostics and persistent application configuration.

Physical backlight mutation is stricter: `POST /api/backlight` is accepted only
when both the TCP peer and the literal HTTP Host are loopback. The API accepts a
bounded logical percentage or `idle`/`active` mode, never a device path or raw
HID report.

## Owner authentication

The local kiosk is treated as owner only when its remote address is loopback
and Host is `localhost`, `127.0.0.0/8` or `::1`. Requiring both prevents a web
page using a DNS-rebound hostname from inheriting loopback trust.

Remote owner calls require a long random token from `OWNER_TOKEN` or
`security.owner_token`. It can be supplied as either:

```text
Authorization: Bearer <token>
X-Owner-Token: <token>
```

`POST /api/auth/owner` can exchange it for a signed, HttpOnly, SameSite=Lax
session. Auth API responses are `Cache-Control: no-store`. Generate the value
with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

There is intentionally no default/shared password. If no owner token is
configured, non-loopback owner access is unavailable.

## OAuth and shared-display pairing

Playback does not require OAuth. Private crate personalization uses an explicit
owner-approved flow:

1. An owner or the trusted local kiosk mints a cryptographically random join
   URL. It expires after 10 minutes and is stored only as a digest.
2. The URL can be consumed once. Consumption permits one OAuth initiation for
   at most 5 minutes and cannot be promoted to owner scope by query parameters.
3. OAuth uses one-time `state`, PKCE S256 and a 10-minute flow lifetime.
4. A paired guest grant replaces the preceding personalized account and expires
   after `guest_session_hours` (12 hours by default, bounded to 1–168).
5. Disconnect/account replacement clears private caches and invalidates
   in-flight crate publication by account generation.

OAuth requires a canonical `public_base_url`, for example
`https://display.example`. `redirect_uri` must be the exact same origin with
path `/callback`, no query and no fragment. The browser must also arrive on the
configured public origin. This supports a TLS reverse proxy without trusting
client-controlled `X-Forwarded-*` headers. The session cookie's `Secure` flag is
derived from that canonical public origin.

Register the exact callback in the Spotify application. Do not use a public
origin that forwards directly from the Internet to this appliance.

## Browser request protections

- State-changing requests require an allowed Origin when an Origin is present;
  accepted origins are the direct request origin and the explicitly configured
  public origin.
- Request bodies are globally size-bounded. Route JSON shapes, strings,
  collections, hosts, numeric ranges and floating-point finiteness are checked.
- Rate buckets are finite and endpoint-specific. Continuous kiosk volume and
  brightness gestures have a higher but bounded allowance; administrative
  mutation is stricter.
- Public SSE connections are atomically capped so slow clients cannot occupy all
  Waitress workers. Polling remains the browser fallback.
- Framing and content sniffing are disabled by response headers; the content
  security policy remains compatible with the kiosk's local inline renderer.

## Secrets and persistence

- `config.json` must be mode `0600`; refresh/client/owner tokens are never
  returned by public APIs or logs.
- Durable writes are locked, written to a restrictive temporary file, fsynced
  and atomically replaced. An existing malformed, oversized, wrong-shaped or
  unreadable config is reported as degraded and is never overwritten by an API
  update.
- Runtime state is no-follow, regular-file, size/schema/type checked under
  `/run/spotify-display` and is not trusted as configuration; the old `/tmp`
  path is read only through the same bounds during migration.
- Runtime dependency installation uses `requirements.lock --require-hashes`;
  the optional release-test toolchain has its own `requirements-test.lock`.
  Receiver and bootstrap downloads have pinned checksums.

Configuration health reports only a bounded state such as `valid`, `malformed`
or `unreadable`, not file contents. Repair a corrupt file from the console after
making a byte-for-byte backup.

## Deployment controls

- Never port-forward the Flask/Waitress port from the router.
- Prefer an appliance VLAN/firewall that prevents guest and untrusted IoT
  networks from reaching owner routes.
- If remote owner/OAuth access is needed, terminate HTTPS at one deliberately
  configured reverse proxy and allow only that origin.
- Keep the Pi patched, retain mode 0600 on config/backups, and do not put tokens
  in shell history or service command lines.
- Treat local control endpoints as intentional LAN controls, not as evidence
  that every LAN client is trusted with private library data.

## Incident response

If a client secret, owner token or refresh token may have leaked:

1. Remove outside access and preserve relevant redacted journals.
2. Revoke the Spotify grant/application credential as applicable.
3. Rotate the owner token and client secret outside shell history.
4. Disconnect the personalized account and restart the Flask service to clear
   in-memory tokens/caches.
5. Confirm configuration mode/ownership and audit reverse-proxy/router rules.

Diagnostics may contain status, ages, counters, temperatures, load and error
categories. They must never contain bearer/refresh tokens, OAuth codes, Wi-Fi
credentials or private upstream payloads.
