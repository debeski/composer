# Manual first-deploy setup

With Composer 1.5.3+ and DjangoLux 1.9.4+, start an unconfigured deployment with:

```sh
./start.sh --skip-config
# Development mode:
./start.sh -d --skip-config
```

Composer injects `DLUX_SKIP_CONFIG_IMPORT=True` through its runtime Compose
override into every service. Native `pre_start` migrators inherit it, as do
legacy post-start commands and the web service. DjangoLux skips automatic
`config.json` import during both migration bootstrap and setup-page requests.
The file remains untouched. Migrations, static collection, admin bootstrap,
manual wizard completion, and explicitly requested settings imports still run.
Existing settings and business data are never reset.

The option also works with `./start.sh update --skip-config`. It lasts for the
containers created by this deployment, including their restarts. Repeat it on
every deployment/recreation until manual setup is complete; a later deploy
without it restores the normal auto-import behavior for an unconfigured DB.
Once setup is complete, DjangoLux already ignores automatic config bootstrap.
For a permanent policy, declare `DLUX_SKIP_CONFIG_IMPORT=True` in the app's web
and migrator service environments in Compose. Merely exporting it on the host
does not inject it into containers.

Older DjangoLux releases do not recognize this variable and still auto-import
the file. Upgrade the app image before relying on the option.
