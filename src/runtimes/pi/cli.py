"""CLI commands for installing and inspecting runtime assets."""

from __future__ import annotations

import json

import click


@click.group("runtime")
def runtime() -> None:
    """Install, inspect and remove Agent runtime assets."""


@runtime.command("install")
@click.argument("runtime_id", type=click.Choice(["pi", "execution-owner"]))
def runtime_install(runtime_id: str) -> None:
    """Install locked dependencies for the selected runtime."""

    if runtime_id == "execution-owner":
        try:
            from agentloom.execution.durable.openclaw_install import install_openclaw
            entry = install_openclaw()
        except (ImportError, RuntimeError) as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(f"OpenClaw process assets ready: {entry}")
        return

    from agentloom.runtimes.pi.install import install_pi
    from agentloom.runtimes.pi.metadata import SDK_VERSION

    try:
        entry = install_pi()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Pi SDK {SDK_VERSION} ready: {entry}")


@runtime.command("status")
@click.argument("runtime_id", type=click.Choice(["pi"]))
def runtime_status(runtime_id: str) -> None:
    """Report whether the Pi runtime is installed and ready."""

    from agentloom.runtimes.pi.install import pi_runtime_status

    click.echo(
        json.dumps(
            pi_runtime_status(),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@runtime.command("uninstall")
@click.argument("runtime_id", type=click.Choice(["pi"]))
def runtime_uninstall(runtime_id: str) -> None:
    """Remove installed assets for the Pi runtime."""

    from agentloom.runtimes.pi.install import uninstall_pi

    removed = uninstall_pi()
    click.echo(f"Removed Pi runtime assets: {removed}")
