"""Thin entry point -- see ``guitars.management.enforcement`` for the implementation. Keeps
``manage.py makeguitarmigrations`` unaffected by that package's internal split."""

from __future__ import annotations

from guitars.management.enforcement.command import Command


__all__ = ['Command']
