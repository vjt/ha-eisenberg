# Brand assets

The icons are a square crop of the repository's cover image,
[`docs/hero.jpg`](../../../docs/hero.jpg): the skater on the rail. The PNGs
are rendered from it and should never be edited by hand:

```sh
cd custom_components/eisenberg/brand
for size in 256 512; do
  out=$([ "$size" = 256 ] && echo icon.png || echo icon@2x.png)
  magick ../../../docs/hero.jpg -crop 620x620+250+20 +repage \
    -filter Lanczos -resize ${size}x${size} \
    -dither FloydSteinberg -colors 256 -strip PNG8:"$out"
done
```

The 256-colour palette is indistinguishable from full colour on this image and
keeps the files a third of the size.

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

The same image as the README and the GitHub cover, so the integration looks
like itself everywhere. The crop keeps the whole skater, skates on the rail,
because at roughly 40px in the integrations list a figure still reads where
the circuit board and the camera irises around it do not.
