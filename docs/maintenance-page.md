# Maintenance page

The page the proxy shows while DjangoLux is in maintenance or `web` is down ships
in the Composer image (`composer/assets/maintenance/`: `index.html`,
`maintenance.css`, `maintenance.js`, no inline script or style).

## How it is served

`composer-agent` publishes the page on every start, and checks it again every 10
seconds, to the runtime volume at `proxy/maintenance/`, which the proxy already
mounts read-only, so an `agent update` refreshes it everywhere. The proxy serves the
page from there on 502/503/504 and its assets under `/_dlux/maintenance/`, which
bypasses `web` so they load during an outage (DjangoLux stack schema 3).

The app services can write the runtime volume, so only Composer decides what that
folder holds:

- the folder is `0755` and its files `0644`, owned by the agent: a non-root app
  service (group 0) can read them but not change them;
- on each check the agent puts back any file that differs, moves anything it did not
  publish to `proxy/maintenance.refused/`, and replaces the folder itself if it was
  swapped for another folder or a link;
- the source is the project's `.proxy/maintenance/` (the agent mounts the project
  read-only) or, without one, the bundled page. The `.custom` marker on the volume
  is written for `check` to read and never decides anything, except on a stack whose
  agent cannot see the project, where only a marker the agent itself wrote counts.

The bundled page is neutral (the site is temporarily unavailable) and says an update
is in progress only while `/_update/status.json` reports an in-progress update phase,
with no `error`, updated in the last 15 minutes. A failed or ended run (`failed`,
`rolled_back`, `ready`, `completed`, or a status the page does not know) gets the
neutral text; the page waits for the site and returns to it. It follows
the visitor's browser language: Arabic (right to left) or English.

## A project's own page

A project that wants its own page keeps it in `.proxy/maintenance/` (an
`index.html` plus any assets). The agent publishes it on its own; it is copied only
when it is safe to serve to anyone:

- a symbolic link, a FIFO or another special file, a file over 1 MiB, more than 64
  files or 8 MiB in all, a folder without `index.html`, or a folder that is itself a
  link or lies outside the project refuses the whole page: nothing is copied, the
  agent logs why and serves the bundled page;
- dotfiles, subfolders and files other than `.html`, `.htm`, `.css`, `.js`, `.mjs`,
  images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`, `.avif`, `.ico`) and
  fonts (`.woff`, `.woff2`, `.ttf`, `.otf`) are never published.

`composer maintenance publish --override DIR` (inside `composer-agent`) publishes a
folder at once, under the same rules, and lists what it skipped;
`composer maintenance publish` republishes from the project or the bundle. Both
refuse to run where the runtime volume is not mounted (the deployer behind
`./start.sh`), since nothing written there reaches the proxy:

```bash
docker compose exec composer-agent python -m composer maintenance publish
```

The proxy sends this Content-Security-Policy with the page and its assets (the
`/_dlux/maintenance/*` route and the 502/503/504 error page):

```text
default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'
```

A project page therefore loads its styles, scripts, images and fonts from its own
folder (`/_dlux/maintenance/...`) and has no inline `<script>`, `style=""` or event
handler; `fetch()` reaches only the site itself (`/_update/status.json`,
`/_update/log.txt`).

## Migrating a stack from before schema 3

Stacks generated before schema 3 serve `.proxy/maintenance.html` from a bind mount.
`check` reports that as a `maintenance-page` warning and `check --fix` (resident
agent 1.7+) migrates it:

1. it rewrites `.proxy/Caddyfile` / `.proxy/default.conf.template` and drops the
   mount from compose;
2. it validates the new proxy config in a throwaway container before changing
   anything;
3. it keeps a customised page as the `.proxy/maintenance/` override;
4. it archives the originals under `.xclude/composer-check/`, publishes through the
   agent and recreates the proxy.

The files are restamped 2 → 3 ([stamps](stack-migrations.md#stamps)).
`composer/assets/maintenance.html.tmpl` is kept only to recognise a stock legacy
page.
