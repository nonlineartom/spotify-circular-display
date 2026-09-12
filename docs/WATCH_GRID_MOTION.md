# Album browser motion

The prototype targets the classic, freely pannable Apple Watch honeycomb, adapted
to the album window inside a 1080px circular display. These are approximations of
the visible behaviour; Apple has not published its Home Screen projection or
physics constants.

## Research

- [Apple: adjust brightness, text size and motion](https://support.apple.com/en-gb/guide/watch/apd766b7bd85/watchos)
  says Reduce Motion makes Home Screen grid icons the same size. The browser
  therefore uses equal-sized covers and immediate movement when the operating
  system requests reduced motion.
- [Apple Watch user guide, watchOS 9, page 61](https://help.apple.com/pdf/watch/9/en_US/apple-watch-user-guide-watchos9.pdf)
  documents the classic grid's tap interaction and opening the central app with
  the Crown. This project retains direct album selection and uses pinch/buttons
  for zoom because the display has no Crown.
- [Sab1e / LVGL: recreating the bubble component](https://lvgl.io/blog/tutorial-recreating-apple-watch-bubble-component)
  describes size changing with position, inward edge compaction, inertia,
  rebound and pressed feedback in an original recreation.
- [Matt Carroll: Apple Watch app grid](https://renderobjects.com/examples/apple-watch-app-grid/)
  demonstrates combining a smooth size falloff with its integral to compress
  positions near the edge. The prototype uses that mathematical approach with
  an elliptical boundary, fitting the available album window.

## Behaviour

Each cover has a fixed position on a hexagonal lattice. After pan and zoom, its
distance from the viewing centre determines both scale and inward displacement.
The central 30% keeps full-sized artwork. A smooth cubic falloff compresses the
outer region towards the boundary. The actual buttons receive the same transforms
as their art, so hit testing follows what is visible.

Swipes coast with decaying velocity and return elastically from finite collection
bounds. Touching during a coast stops it without opening an album. Pinches keep
the content under their midpoint anchored by inverting the lens projection.
Centre eases back to the starting view. No perpetual animation loop runs at rest.

Mosaic pages hold at most 37 covers; the labelled grid still shows six at a time.
Search, switching collection/layout, opening album details, closing the browser
and hiding the document cancel motion. Off-screen covers remain reachable by
keyboard focus, which brings their artwork to the centre.

Validation combines pure geometry/physics tests with browser interaction at
1080×1080. The physical Pi still needs its own frame-rate and finger-feel check.
