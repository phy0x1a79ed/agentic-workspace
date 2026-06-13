"""Entry point for `python -m awm`."""

from awm.gateway.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
