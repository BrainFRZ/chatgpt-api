"""APScheduler integration for the Characters continuous-existence layer.

Runs background jobs in the FastAPI process on the VPS:
- Daily resolver: 8am / 1pm / 6pm ET per Characters-enabled project
- Weekly planner: Sunday 8pm ET per project (Phase 3 — registered later)

One pair of jobs per project. Discovered on startup by walking
data/users/*/projects/* for those with a `character_profile.di` file.

Persistent jobstore at `data/.scheduler.db` (SQLite via SQLAlchemy) so jobs
survive process restarts. Misfire policy: skip-on-miss with a 1hr grace
window; if the VPS was down at 8am we don't double-run later.

Anthropic API key is read from `ANTHROPIC_API_KEY` env var (the SDK picks it
up automatically). Cron jobs run unattended — no per-request API key.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-imported to avoid hard requirement at import time. The actual scheduler
# is created in init_scheduler() which is called from FastAPI lifespan.
_SCHEDULER = None
_TZ_NAME = "America/New_York"

DAILY_RESOLVER_HOURS = (8, 13, 18)   # ET
WEEKLY_PLANNER_DAY = "sun"
WEEKLY_PLANNER_HOUR = 20

# Jobstore lives OUTSIDE data/ on purpose: data/ syncs P2P via Syncthing,
# and a synced SQLite jobstore would let multiple devices fire each other's
# jobs. By anchoring relative to scheduler.py and putting it under var/ at
# the repo root, each device has its own jobstore. The VPS runs FastAPI in
# production; dev boxes that import scheduler.py get their own var/ created
# locally without conflict.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBSTORE_DIR = os.path.join(_REPO_ROOT, "var")
JOBSTORE_PATH = os.path.join(JOBSTORE_DIR, "scheduler.db")

# Absolute path to the data root, anchored relative to scheduler.py. Avoids
# cwd-dependent behavior — FastAPI runs from backend/ on the VPS, so a
# relative "data/" would resolve to backend/data/ which doesn't exist.
_DEFAULT_DATA_ROOT = os.path.join(_REPO_ROOT, "data")


def _scheduler_instance():
    return _SCHEDULER


def init_scheduler() -> Optional[object]:
    """Create the AsyncIOScheduler singleton with persistent jobstore. Returns
    the scheduler (started) or None if APScheduler isn't installed.
    """
    global _SCHEDULER
    if _SCHEDULER is not None:
        return _SCHEDULER
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    except ImportError:
        logger.warning("scheduler: apscheduler not installed — background jobs disabled. "
                       "Run `pip install apscheduler sqlalchemy` to enable.")
        return None

    os.makedirs(JOBSTORE_DIR, exist_ok=True)
    jobstores = {
        "default": SQLAlchemyJobStore(url=f"sqlite:///{JOBSTORE_PATH}"),
    }
    _SCHEDULER = AsyncIOScheduler(jobstores=jobstores, timezone=_TZ_NAME)
    _SCHEDULER.start()
    logger.info(f"scheduler: started, jobstore={JOBSTORE_PATH}, tz={_TZ_NAME}")
    return _SCHEDULER


def shutdown_scheduler() -> None:
    global _SCHEDULER
    if _SCHEDULER is None:
        return
    try:
        _SCHEDULER.shutdown(wait=False)
        logger.info("scheduler: stopped")
    except Exception as e:
        logger.warning(f"scheduler: shutdown error: {e}")
    _SCHEDULER = None


def discover_characters_projects(data_root: Optional[str] = None) -> list[str]:
    """Walk data/users/*/projects/* and return paths to projects that have a
    `character_profile.di` (i.e. completed Characters interview). Used to
    enumerate which projects need scheduled jobs.
    """
    out: list[str] = []
    if not data_root:
        data_root = _DEFAULT_DATA_ROOT
    users_root = os.path.join(data_root, "users")
    if not os.path.isdir(users_root):
        logger.warning(f"scheduler: users root not found at {users_root}")
        return out
    for user in os.listdir(users_root):
        proj_root = os.path.join(users_root, user, "projects")
        if not os.path.isdir(proj_root):
            continue
        for project in os.listdir(proj_root):
            project_dir = os.path.join(proj_root, project)
            if not os.path.isdir(project_dir):
                continue
            if os.path.isfile(os.path.join(project_dir, "character_profile.di")):
                out.append(os.path.abspath(project_dir))
    return out


def _resolver_job_id(project_dir: str) -> str:
    # Stable id derived from absolute project path; APScheduler's
    # replace_existing makes registering idempotent.
    return f"daily_resolver:{os.path.abspath(project_dir)}"


def _planner_job_id(project_dir: str) -> str:
    return f"weekly_planner:{os.path.abspath(project_dir)}"


def register_resolver_for_project(project_dir: str) -> bool:
    """Register the daily-resolver cron job for one project. Idempotent —
    re-registering replaces the existing entry.

    Returns True if registered, False if scheduler unavailable.
    """
    sched = _scheduler_instance()
    if sched is None:
        return False
    from apscheduler.triggers.cron import CronTrigger

    project_dir = os.path.abspath(project_dir)
    job_id = _resolver_job_id(project_dir)
    trigger = CronTrigger(
        hour=",".join(str(h) for h in DAILY_RESOLVER_HOURS),
        minute=0,
        timezone=_TZ_NAME,
    )
    sched.add_job(
        _run_resolver_job_async,
        trigger=trigger,
        args=[project_dir],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,  # 1hr grace; skip if older than that
        coalesce=True,            # if multiple fires queued, only run once
        max_instances=1,          # never overlap a job for the same project
    )
    logger.info(f"scheduler: registered {job_id} at {DAILY_RESOLVER_HOURS} ET")
    return True


def register_planner_for_project(project_dir: str) -> bool:
    """Register the weekly-planner cron job for one project. Idempotent —
    re-registering replaces the existing entry. Sundays at 20:00 ET.
    """
    sched = _scheduler_instance()
    if sched is None:
        return False
    from apscheduler.triggers.cron import CronTrigger

    project_dir = os.path.abspath(project_dir)
    job_id = _planner_job_id(project_dir)
    trigger = CronTrigger(
        day_of_week=WEEKLY_PLANNER_DAY,
        hour=WEEKLY_PLANNER_HOUR,
        minute=0,
        timezone=_TZ_NAME,
    )
    sched.add_job(
        _run_planner_job_async,
        trigger=trigger,
        args=[project_dir],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600 * 6,  # 6hr grace; planner only runs once a week
        coalesce=True,
        max_instances=1,
    )
    logger.info(f"scheduler: registered {job_id} at {WEEKLY_PLANNER_DAY} {WEEKLY_PLANNER_HOUR}:00 ET")
    return True


def register_all_projects(data_root: Optional[str] = None) -> int:
    """Walk all Characters-enabled projects and register their resolver +
    planner jobs. Returns the number of projects registered.
    """
    sched = _scheduler_instance()
    if sched is None:
        return 0
    projects = discover_characters_projects(data_root)
    n = 0
    for p in projects:
        ok_resolver = register_resolver_for_project(p)
        ok_planner = register_planner_for_project(p)
        if ok_resolver or ok_planner:
            n += 1
    logger.info(f"scheduler: registered jobs for {n} projects")
    return n


async def _run_resolver_job_async(project_dir: str) -> None:
    """The actual job function APScheduler invokes. Wraps blocking SDK calls
    in a thread so we don't stall the AsyncIO scheduler's event loop.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error(
            f"scheduler: ANTHROPIC_API_KEY not set — resolver for {project_dir} "
            f"cannot run. Set it in the VPS env."
        )
        return
    try:
        meta = await asyncio.to_thread(_run_resolver_blocking, project_dir)
        logger.info(f"scheduler: resolver pass for {os.path.basename(project_dir)}: {meta}")
    except Exception as e:
        logger.error(f"scheduler: resolver job for {project_dir} crashed: {type(e).__name__}: {e}")


def _run_resolver_blocking(project_dir: str) -> dict:
    """Sync wrapper: build Anthropic client + call run_daily_resolver."""
    import anthropic
    from character_resolver import run_daily_resolver
    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env
    return run_daily_resolver(client, project_dir)


async def _run_planner_job_async(project_dir: str) -> None:
    """APScheduler-invoked job: weekly planner pass. Wraps blocking SDK calls."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error(
            f"scheduler: ANTHROPIC_API_KEY not set — planner for {project_dir} cannot run."
        )
        return
    try:
        meta = await asyncio.to_thread(_run_planner_blocking, project_dir)
        logger.info(f"scheduler: planner pass for {os.path.basename(project_dir)}: {meta}")
    except Exception as e:
        logger.error(f"scheduler: planner job for {project_dir} crashed: {type(e).__name__}: {e}")


def _run_planner_blocking(project_dir: str) -> dict:
    """Sync wrapper: build Anthropic client + call run_weekly_planner."""
    import anthropic
    from character_planner import run_weekly_planner
    client = anthropic.Anthropic()
    return run_weekly_planner(client, project_dir)


async def trigger_planner_now(project_dir: str) -> dict:
    """On-demand planner pass for one project. Used by interview-finalize to
    seed the initial week for a brand new character without waiting for Sunday."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "ANTHROPIC_API_KEY not set"}
    return await asyncio.to_thread(_run_planner_blocking, project_dir)


# ── On-demand catch-up (called from session-resume path in main.py) ─

async def trigger_resolver_now(project_dir: str) -> dict:
    """Run the resolver immediately for one project. Used when a chat session
    opens after a long gap and the cron hasn't covered the elapsed time yet.
    Bypasses the scheduler — runs the same function directly in a thread.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "ANTHROPIC_API_KEY not set"}
    return await asyncio.to_thread(_run_resolver_blocking, project_dir)
