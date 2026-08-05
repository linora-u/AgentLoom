"""Tool packages.

This package intentionally exports nothing.  Import concrete tools from their
own package, read built-in metadata from :mod:`src.tools.catalog`, and resolve
registered implementations through :mod:`src.tools.loader`.
"""

__all__: list[str] = []
