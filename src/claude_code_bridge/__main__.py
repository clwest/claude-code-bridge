"""Module entry point so `python -m claude_code_bridge` runs the server."""

from .server import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
