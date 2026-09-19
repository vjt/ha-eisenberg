# Brand assets

`icon.svg` is the source. The PNGs are rendered from it and should never be
edited by hand:

```sh
cd custom_components/eisenberg/brand
rsvg-convert -w 256 -h 256 icon.svg -o icon.png
rsvg-convert -w 512 -h 512 icon.svg -o icon@2x.png
```

## Why they live here

Home Assistant 2026.3 onwards reads brand images straight out of a custom
integration's own `brand/` directory, and local images take priority over the
brands CDN. Recognised names are `icon.png`, `icon@2x.png`, `logo.png`,
`logo@2x.png` and their `dark_` variants. See
<https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/>.

This replaces submitting to [`home-assistant/brands`](https://github.com/home-assistant/brands),
which no longer accepts brand icons for custom integrations
([PR #10970](https://github.com/home-assistant/brands/pull/10970)).

So this directory is not a staging area for a submission elsewhere — it is what
Home Assistant actually serves. It must ship with the integration.

## The design

A camera iris at the hub with three cameras wired into it: one event stream
fanning out to every camera on the account. Flat geometry, because Home
Assistant renders it at roughly 40px in the integrations list, where the only
thing left to say "camera" rather than "generic network node" is the pinwheel
of the aperture.
