# QR encoder

`qrcode-generator-1.4.4.js` is an unmodified copy of `qrcode.js` from
[`qrcode-generator` 1.4.4](https://www.npmjs.com/package/qrcode-generator/v/1.4.4),
by Kazuhiko Arase. Upstream: <https://github.com/kazuhikoarase/qrcode-generator>.
It is distributed under the MIT license, included in
`qrcode-generator-LICENSE.txt` from the upstream repository.

The npm package tarball was checked against its registry integrity value:
`sha512-HM7yY8O2ilqhmULxGMpcHSF1EhJJ9yBj8gvDEuZ6M+KGJ0YY2hKpnXvRD+hZPLrDVck3ExIGhmPtSdcjC+guuw==`.

The owner page uses only the encoder's in-memory matrix API. It draws its
own SVG with a four-module quiet zone. Pairing links never leave the browser
for QR generation. No runtime package install or CDN is required.

## Gradient backdrops

`gradient-backdrops.js` comes from the local `gradient-backdrops` skill's
`assets/backdrop.js` (2026-10-05). It retains its technique credits in the
header. The appliance copy adds `maxFps` to cap shader draws and explicitly
stops its scheduler while the document is hidden. It also adds a project-specific `heat`
style (sun bloom, rays and horizon shimmer) used for hot, clear weather. No external assets or
dependencies are loaded. `static/idle-backdrop.js` mounts just one 378×378
scene in idle mode, re-parameterises it from forecast values on that same context,
releases it on exit and reduces quality under frame pressure.
