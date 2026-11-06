# Database migrations

Run `./.venv/bin/alembic upgrade head` before starting the API or worker.
SQLite migrations use Alembic batch mode so the same model can later be
implemented by the PostgreSQL persistence adapter.
