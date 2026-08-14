from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version('django-guitars')
except PackageNotFoundError:  # running from a source tree with no installed metadata
    __version__ = '0.0.0+unknown'


class GuitarsError(Exception):
    """Base for every exception guitars raises deliberately -- ``except
    guitars.GuitarsError`` catches all of them without also catching Django's own. See
    :mod:`guitars.tenancy.scope` for the tenancy-specific subclasses."""


__all__ = ['GuitarsError', '__version__']
