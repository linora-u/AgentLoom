"""Explicit source evidence for tests that stub the network worker."""


def verified_sources(day="2025-07-31", url="https://example.org/original"):
    return [{"url": url, "published_at": day, "version_available_at": day,
             "excerpt": "可核实的新执行条款"}]
