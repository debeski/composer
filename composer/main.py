import sys
from pathlib import Path


def main():
    from .launcher import DockerComposeLauncher

    DockerComposeLauncher().run()


if __name__ == "__main__":
    if __package__ is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from composer.launcher import DockerComposeLauncher

        DockerComposeLauncher().run()
    else:
        main()
