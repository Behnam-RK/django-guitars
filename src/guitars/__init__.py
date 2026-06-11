from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version('django-guitars')
except PackageNotFoundError:  # running from a source tree with no installed metadata
    __version__ = '0.0.0+unknown'
