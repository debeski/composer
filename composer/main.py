import sys
from pathlib import Path


def line_buffer_stdout():
    """Keep stdout and stderr in order when both go to one pipe or log."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(line_buffering=True)
    except (OSError, ValueError):
        pass


def main():
    from .launcher import DockerComposeLauncher

    line_buffer_stdout()
    DockerComposeLauncher().run()


if __name__ == "__main__":
    if __package__ is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from composer.launcher import DockerComposeLauncher

        line_buffer_stdout()
        DockerComposeLauncher().run()
    else:
        main()
