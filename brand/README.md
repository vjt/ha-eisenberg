# Brand assets

Source of truth for the Eisenberg icon. `icon.svg` is the original; the PNGs
are rendered from it with `rsvg-convert` and should never be edited by hand:

```sh
rsvg-convert -w 256 -h 256 brand/icon.svg -o brand/icon.png
rsvg-convert -w 512 -h 512 brand/icon.svg -o brand/icon@2x.png
```

## These files do nothing on their own

Home Assistant does not read an icon out of a custom integration's directory.
There is no `icon` key in its manifest schema (`homeassistant/loader.py`,
`class Manifest`), and no local lookup — the frontend resolves every
integration's logo from `brands.home-assistant.io`, which is served from the
[`home-assistant/brands`](https://github.com/home-assistant/brands) repository.

So the icon only appears in the UI once these two PNGs are submitted there as:

```
custom_integrations/eisenberg/icon.png     256x256
custom_integrations/eisenberg/icon@2x.png  512x512
```

Both must be square, trimmed of surrounding whitespace, and carry an alpha
channel (ours is transparent outside the rounded square). Keeping the source
here means that PR is a copy, not a redraw.

## The design

A camera iris at the hub with three cameras wired into it: one event stream
fanning out to every camera on the account. It is drawn as flat geometry
because Home Assistant renders it at roughly 40px in the integrations list,
where every fine detail is gone and only the pinwheel of the aperture is left
to say "camera" rather than "generic network node".
