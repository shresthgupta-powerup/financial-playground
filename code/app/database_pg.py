"""HANDOFF STUB -- NOT the application's real module.

In the INF_APP_BETA repo, `app/database_pg.py` opens real PostgreSQL
connections (it needs secrets from `app/config.py`, which is deliberately
NOT included in this handoff). This stub exists ONLY so that the app-coupled
planning files (`service.py`, `glide_paths_repo.py`, `plans_repo.py`) remain
IMPORTABLE in a standalone environment -- their DB code paths raise the
moment they are actually exercised.

Everything you need for a standalone playground is DB-free:
  - the engine itself:      app/planning/engine.py
  - input validation:       app/planning/validation.py
  - glide-path data:        app/planning/glide_paths.py  (get_glide_paths())
  - the input schema:       app/planning/schemas.py

See 00_START_HERE.md ("Portable vs app-coupled") for the full map.
"""

_MSG = (
    "app.database_pg is a handoff stub -- there is no database in this "
    "package. Use the DB-free path instead: engine.find_retirement_date / "
    "engine.run_simulation with glide_paths.get_glide_paths(). "
    "See 00_START_HERE.md."
)


def execute_query_pg(sql, params=None):
    raise RuntimeError(_MSG)


def execute_returning_pg(sql, params=None):
    raise RuntimeError(_MSG)


def get_pg_connection():
    raise RuntimeError(_MSG)
