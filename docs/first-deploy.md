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

## Secrets access differs between processes

The terminal launcher and deployment executor can map to different host UIDs,
especially with Docker user-namespace remapping. A readable file in your shell
therefore does not establish that the executor can read it. `./start.sh check`
verifies executor access too. Operations routes its read-only deployment check
through that executor; the network-facing agent does not need secrets access.

An unreadable file prevents the executor from validating deployment configuration
and can block recreates/updates that need those values. The finding identifies
the affected host UID and prints specific `sudo setfacl` commands for the Linux
VM. Run those commands in the VM project directory. sudo asks for the VM account
password locally, never in DjangoLux. Install the host's `acl` package if needed,
then rerun `./start.sh check`. Keep the file restricted; do not make it
world-readable or give the network-facing agent access to silence a warning.
