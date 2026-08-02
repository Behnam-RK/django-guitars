"""Mirror `.github/workflows/ci.yml`'s test matrix locally.

Same three axes (Python, Django, PostgreSQL) as CI, same exclude, same
commands -- kept in one place only, this file re-derives nothing and
hardcodes nothing CI doesn't already hardcode. See ci.yml's matrix comment
for the *why* of each axis and the exclude; if that changes, mirror it here.

    uv run nox                                  # every valid cell
    uv run nox -s "test(python='3.12', django='5.2', postgres='18')"
    uv run nox -k "postgres='14'"                # every cell against Postgres 14
"""

from __future__ import annotations

import os

import nox


nox.options.default_venv_backend = 'uv'
nox.options.reuse_existing_virtualenvs = True

PYTHON_VERSIONS = ['3.10', '3.12', '3.14']
DJANGO_VERSIONS = ['5.0', '5.2', '6.0']
POSTGRES_VERSIONS = ['14', '18']

# Django 6.0 requires Python >=3.12 -- mirrors ci.yml's matrix.exclude exactly;
# every other cell in the cross product installs (see ci.yml's comment on how
# that was verified) and is what the rest of the matrix finds out.
CELLS = [
    (python, django, postgres)
    for python in PYTHON_VERSIONS
    for django in DJANGO_VERSIONS
    for postgres in POSTGRES_VERSIONS
    if (python, django) != ('3.10', '6.0')
]


@nox.session(venv_backend='uv')
@nox.parametrize('python,django,postgres', CELLS)
def test(session: nox.Session, python: str, django: str, postgres: str) -> None:
    """Run the suite for one (python, django, postgres) cell -- same steps as CI."""
    env = {
        **os.environ,
        'UV_PROJECT_ENVIRONMENT': session.virtualenv.location,
        'DJANGO_SETTINGS_MODULE': 'tests.settings',
        'POSTGRES_VERSION': postgres,
    }

    # `uv sync --locked` first (every other dependency, from the committed
    # lockfile), then `uv pip install` overrides just Django for this cell --
    # deliberately not `uv run --with`, for the same reason ci.yml gives: every
    # later command in this session needs to see the same overridden Django.
    session.run_install('uv', 'sync', '--locked', f'--python={python}', env=env)
    session.run_install('uv', 'pip', 'install', f'Django=={django}.*', env=env)

    session.run('docker', 'compose', 'up', '-d', '--wait', env=env, external=True)
    try:
        session.run('pytest', '--cov=guitars', '--cov-report=term-missing', env=env, external=True)
        session.run('python', 'manage.py', 'makemigrations', '--check', env=env, external=True)
        session.run('python', 'manage.py', 'migrate', env=env, external=True)
        session.run(
            'python',
            'manage.py',
            'audittenancy',
            '--require-force',
            '--require-match',
            env=env,
            external=True,
        )
    finally:
        session.run('docker', 'compose', 'down', env=env, external=True)
