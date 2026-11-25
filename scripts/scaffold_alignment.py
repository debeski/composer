"""Gate: a freshly generated DjangoLux project needs nothing from `composer check --fix`.

DjangoLux's scaffold and Composer's repairs describe the same stack. When one
changes without the other, a new project is born with findings, or `check --fix`
rewrites files the scaffold just wrote. This generates a project from a DjangoLux
checkout and runs this checkout's `check` and repair preview on it:

- no finding may FAIL;
- a WARN is allowed only when it depends on the machine or on a live stack
  (``ALLOWED_WARNINGS``), never on the files the scaffold wrote;
- no repair may plan a file change;
- the generated `start.sh` / `start.ps1` are byte-identical to this checkout's.

Run it before tagging a paired DjangoLux + Composer beta:

    python scripts/scaffold_alignment.py --dlux ../../pkg-django-lux/<checkout> \\
        --dlux-python ../../pkg-django-lux/main/.venv/bin/python

It needs Docker (for `docker compose config`) and writes only to a temporary
directory. Exit 0 means aligned.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
WRAPPERS = ("start.sh", "start.ps1")

# Findings a fresh project cannot clear without a deployment or an operator's
# choice; any other WARN means the scaffold and Composer disagree.
ALLOWED_WARNINGS = {
    "secrets": "the executor's access needs a running composer-executor",
    "maintenance-page": "composer-agent publishes the page when the stack runs",
    "front-proxy": "DLUX_FRONT_PROXY is the operator's choice (owner, 1.16.1)",
    "registry-login": "depends on this machine's certificate store and the image name",
    "app-permissions": "warns until the scaffold image runs non-root",
}


def generate(dlux: Path, python: str, target: Path) -> Path:
    """A fresh project from ``dlux``'s scaffold, under a Compose name no other run uses."""
    name = f"align{secrets.token_hex(4)}"
    env = {**os.environ, "PYTHONPATH": str(dlux)}
    subprocess.run([python, "-m", "dlux", "startproject", name], cwd=target, env=env,
                   check=True, capture_output=True, text=True)
    return target / name


def _project_resources(project_name: str) -> set:
    """The Docker networks and volumes Compose labelled with ``project_name``."""
    found = set()
    for kind in ("network", "volume"):
        listing = subprocess.run(
            ["docker", kind, "ls", "--filter", f"label=com.docker.compose.project={project_name}",
             "--format", "{{.Name}}"],
            capture_output=True, text=True,
        )
        found |= {(kind, name) for name in listing.stdout.split() if name}
    return found


def inspect(project: Path) -> dict:
    """This checkout's findings and planned repairs for ``project``.

    ``check`` reads the image's DjangoLux with ``compose run`` and probes data
    access with ``docker run -v``, which create the project's networks and named
    volumes. A deployed stack already has them; this throwaway project does not,
    so the networks and volumes this call created are removed again, and nothing
    that existed before it is touched.
    """
    sys.path.insert(0, str(HERE))
    from composer.launcher import DockerComposeLauncher
    from composer.ops import _dry_run_repairs

    before = _project_resources(project.name)
    previous = os.getcwd()
    os.chdir(project)
    try:
        launcher = DockerComposeLauncher()
        args = SimpleNamespace(
            file=None, dev=False, fix=False, yes=False, deep=False,
            deep_service="web", deep_command="python manage.py dlux_doctor",
            json=True, beta=False, stable=False,
        )
        results, _fixes = launcher.collect_checkup(args)
        repairs, _digest = _dry_run_repairs(launcher, args)
    finally:
        os.chdir(previous)
        for kind, name in sorted(_project_resources(project.name) - before):
            subprocess.run(["docker", kind, "rm", name], capture_output=True, text=True)
    wrappers = [
        name for name in WRAPPERS
        if (project / name).read_bytes() != (HERE / name).read_bytes()
    ]
    return {"results": results, "repairs": repairs, "wrappers": wrappers}


def verdict(report: dict) -> list:
    problems = []
    for result in report["results"]:
        level, name = result.get("level"), result.get("name")
        if level == "fail":
            problems.append(f"FAIL {name}: {result.get('message')}")
        elif level == "warn" and name not in ALLOWED_WARNINGS:
            problems.append(f"WARN {name}: {result.get('message')}")
    for repair in report["repairs"]:
        if repair.get("files"):
            problems.append(f"REPAIR {repair['name']} would rewrite {', '.join(repair['files'])}:\n{repair.get('diff', '')}")
    for name in report.get("wrappers", ()):
        problems.append(f"WRAPPER {name} differs from Composer's (copy it into DjangoLux's dlux/scaffold/templates/project/{name}.tmpl)")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dlux", required=True, help="DjangoLux checkout whose scaffold is checked")
    parser.add_argument("--dlux-python", default=sys.executable, help="Python with DjangoLux's dependencies")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="scaffold-alignment-") as holder:
        project = generate(Path(args.dlux).resolve(), args.dlux_python, Path(holder))
        report = inspect(project)
    problems = verdict(report)
    if args.json:
        print(json.dumps({"problems": problems, **report}, default=str, indent=2))
    else:
        for problem in problems:
            print(problem)
        print("aligned" if not problems else f"{len(problems)} misalignment(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
