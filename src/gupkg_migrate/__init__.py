"""The gupkg 0.17 to 1.0 migration tool, a separate program outside gupkg core.

``gupkg-migrate package`` converts version directories and trackers to format 2,
``gupkg-migrate settings`` converts a manager configuration to ``settings.toml``, and
``gupkg-migrate shortcuts`` imports ``.lnk`` shortcuts. The tool imports gupkg core (decision
D44) and ships in the release bundle; core never imports it and runs without it (D33).
``gupkg migrate ...`` forwards here (D45).
"""
