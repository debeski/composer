# Maintenance page

The page the proxy shows while DjangoLux is in maintenance or `web` is down ships
in the Composer image (`composer/assets/maintenance/`: `index.html`,
`maintenance.css`, `maintenance.js`, no inline script or style).

## How it is served

`composer-agent` publishes the page on every start to the runtime volume at
`proxy/maintenance/`, which the proxy already mounts read-only, so an `agent update`
refreshes it everywhere. The proxy serves the page from there on 502/503/504 and
its assets under `/_dlux/maintenance/`, which bypasses `web` so they load during an
outage (DjangoLux stack schema 3).

## A project's own page

A project that wants its own page keeps it in `.proxy/maintenance/` (an
`index.html` plus any assets):

- `composer maintenance publish --override DIR` copies it over the published page
  and drops a `.custom` marker, which the agent respects;
- `composer maintenance publish` (inside `composer-agent`) republishes the bundled
  page.

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
