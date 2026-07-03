"""Minimal Django settings for the guitars dev/test harness.

`django-guitars` ships a model-only reusable app, so this harness keeps only
what the kit needs: a PostgreSQL database and the `guitars` app. No auth,
sessions, admin, messages, staticfiles, templates, or middleware.
"""

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

# Harness-only key. This project is never deployed; do not reuse in production.
SECRET_KEY = 'django-insecure-harness-key-not-for-production'

DEBUG = True

ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    'guitars',
]

# First-party apps the `makeguitarmigrations` command scans for advanced
# (trigger / rule) migrations. tests/settings.py adds the test app here.
LOCAL_APPS: list[str] = []

# When True (default), `makemigrations` also generates the advanced trigger/rule
# migrations, so `makeguitarmigrations` never has to be run by hand. Set to False
# to keep the explicit two-command workflow.
GUITARS_AUTO_MAKE_MIGRATIONS = True

MIDDLEWARE: list[str] = []

ROOT_URLCONF = 'core.urls'

# The kit's soft-delete rules and updated_at triggers are PostgreSQL-only.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'guitars'),
        'USER': os.environ.get('POSTGRES_USER', 'postgres'),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', 'postgres'),
        'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
        'PORT': os.environ.get('POSTGRES_PORT', '4455'),
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
