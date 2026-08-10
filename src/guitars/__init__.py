from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version('django-guitars')
except PackageNotFoundError:  # running from a source tree with no installed metadata
    __version__ = '0.0.0+unknown'


class GuitarsError(Exception):
    """Base for every exception guitars itself raises deliberately.

    A package-level base so a consumer can ``except guitars.GuitarsError`` to catch
    anything the kit means to raise, without also catching Django's or Python's own
    exceptions. See :mod:`guitars.tenancy.scope` for the tenancy-specific subclasses
    (``TenantScopeError``, ``TenantScopeMissing``, ``TenantScopeViolation``,
    ``TenantValueError``) that inherit from this.
    """


__all__ = ['GuitarsError', '__version__']
