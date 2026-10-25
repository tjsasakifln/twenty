# CONFENGE branding

`confenge-logo-white.png` is the unmodified transparent white CONFENGE wordmark
from [the official website](https://confenge.com.br/assets/logo-confenge-white-500-1677038e.png).
It is intentionally kept as an edge-served asset rather
than a Twenty workspace avatar: the wordmark is horizontal and the workspace
avatar is constrained to a square. Production serves the two public assets from
`/var/www/gestao-confenge-branding`, outside Twenty's private application path.

Nginx injects `confenge-auth.css` into HTML documents, including the root, so
client-side navigation to authentication is covered. Its auth-only selectors
replace Twenty's Android fallback mark and hide its
overlapping workspace badge while retaining the ordinary click target and form.
The CSS uses the fallback icon URL and `/welcome` link, not generated CSS class
names. Install the two assets in the public directory before applying this
virtual host, validate with `nginx -t`, then reload Nginx. Check both `/` and
`/welcome`, plus the PNG and CSS responses. A future Twenty upgrade must verify
that the authentication markup still matches these selectors.

`confenge-mark-white.svg` presents only the symbol from the same official PNG:
the original bytes are embedded unchanged, with a square viewport excluding
the lettering. `confenge-mark-white.png` is its 256 x 256 transparent export.
The white pixels and transparent background were checked before deployment.
To reproduce the PNG, render the SVG with Inkscape:

```sh
inkscape confenge-mark-white.svg --export-type=png --export-width=256 --export-height=256 --export-filename=confenge-mark-white.png
```

Use the PNG with Twenty's native workspace logo upload. Both the sidebar and
the dynamically assigned favicon use that workspace image. SVG is retained as
the reproducible presentation source, not uploaded as a workspace logo: the
native picture UI supports PNG/JPEG/GIF and serves uploaded SVG as attachments.
Nginx also serves the PNG at the document, React fallback and touch-icon paths
so the previous brand cannot appear before workspace data loads. Public files
stay in `/var/www/gestao-confenge-branding`; private directories stay restricted.

To roll back the edge styling, restore the previous virtual host, validate and
reload Nginx. Public files can remain unused. Restore a prior workspace logo
through the native uploader using its private backup; replacing a logo removes
the previous CorePicture, so retain that backup before updating it.
