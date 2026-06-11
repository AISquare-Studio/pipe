"""Allow ``python -m aisquare.pipe.cli`` — used by ``pipe validate --install``
to re-exec in a fresh interpreter after editable installs (their import hooks
only register at startup)."""

from aisquare.pipe.cli.main import cli

if __name__ == "__main__":
    cli()
