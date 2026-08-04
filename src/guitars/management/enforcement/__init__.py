"""The enforcement-migration generator, split by concern -- see ``command.py``'s docstring
for the full account of what lives in each module.
"""

from guitars.management.enforcement.command import Command


__all__ = ['Command']
