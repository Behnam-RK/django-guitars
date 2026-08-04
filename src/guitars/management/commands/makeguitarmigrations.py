"""Thin entry point. See ``guitars.management.enforcement`` for the implementation.

Django's command loader only needs ``Command`` importable from this module path -- the
generator itself was split into a package (``guitars.management.enforcement``) once its
concerns (header templates, identity, scanning, operation building, CLI wiring) each grew
enough to want their own module. This file is what keeps ``manage.py makeguitarmigrations``
unaffected by that reorganization.
"""

from __future__ import annotations

from guitars.management.enforcement.command import Command


__all__ = ['Command']
