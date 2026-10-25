# CONFENGE authentication branding

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

To roll back, restore the previous virtual host (or remove the two branding
locations, HTML filter and added compression overrides), validate and reload
Nginx. The public files can remain unused; no database or container state is
involved.
