# Vacation planner: Streamlit + Supabase

This is a manager-operated vacation planner for the DMC organization:

```text
DMC
└── DMC Department
    ├── ML
    ├── Data Analytics
    └── Data Governance
```

The main repository is Supabase Postgres through SQLAlchemy. The original CSV
repository remains available as a local fallback for demos and offline work.

## 1. Install with uv

Install [uv](https://docs.astral.sh/uv/) once, then from this folder run:

```sh
uv sync
```

Run the test suite and app with uv as well:

```sh
uv run python -m unittest discover -s tests -v
uv run streamlit run app.py
```

There are no pip or requirements.txt steps in this project. `pyproject.toml`
defines dependencies and `uv.lock` pins the resolved environment.

## 2. Create the Supabase tables

1. Create or open the Supabase project.
2. In Supabase **SQL Editor**, run the files in timestamp order:
   `supabase/migrations/20261009120000_vacation_schema.sql`, then
   `supabase/migrations/20261009163000_dmc_department_teams.sql`.
3. The migrations create the private `vacation` schema and DMC hierarchy,
   seed annual/unpaid/training leave types, add
   availability windows, leave requests, balances, memberships, holidays,
   request events, audit-friendly versions, RLS policies, and coverage views.

The first local prototype may connect with the `postgres` database user from
the Supabase Connect dialog. For a deployed app, create a long random password
for the migration's `vacation_app` role and use that least-privilege account.

## 3. Configure the connection

Copy the template and fill in the database password locally:

```sh
cp .env.example .env
```

The supplied project connection values are already in the template. Set
`DB_PASSWORD` to the password from Supabase. Do not commit `.env`.

The app accepts either individual variables:

```dotenv
DATA_BACKEND=supabase
DB_SCHEMA=vacation
DB_USER=postgres
DB_PASSWORD=your-password
DB_HOST=db.lrrozhtefdvklyhfehut.supabase.co
DB_PORT=5432
DB_NAME=postgres
DB_SSLMODE=require
```

or a `DATABASE_URL`. SQLAlchemy's URL builder safely handles special
characters in the password. If you manually assemble a URL, percent-encode
reserved password characters first.

Verify the migration and connection:

```sh
uv run python scripts/test_connection.py
```

If the direct database host is not reachable from an IPv4-only network, copy
the **Session pooler** connection details from Supabase **Connect** instead
(keep port `5432` and `sslmode=require`).

## 4. Use the planner

### Departments and employees

Open **Employees & teams** and choose **DMC hierarchy** or **Employees**.

- The organization is `DMC` → `DMC Department` → `ML`, `Data Analytics`, and
  `Data Governance`. Additional teams can be added under DMC Department.
- Add an employee with an ID, name, and team. Existing DMC-level employees
  remain in the roster as `Unassigned (DMC Department)` until assigned to a team.
- **Remove employee (deactivate)** takes the employee out of the active roster
  without deleting leave history or memberships.
- **Reactivate employee** restores them to the active roster.
- **Permanently delete employee and planner data** appears after deactivation
  and requires typing the employee ID. It erases that employee's availability,
  leave requests and events, balances, and membership history.

### Available dates and leave

In **Date ranges**, add `Available` windows, then add `Planned` leave and move
it to `Approved` or `Cancelled`. The original source records remain intact;
overlapping or consecutive periods are merged for display. Approved leave wins
over Planned leave on the calendar, and availability warnings cover the full
inclusive request interval.

### Calendar and coverage

The calendar counts active employees, approved/planned workdays, holidays, and
department minimum staffing. You can include or exclude Planned leave when
reviewing staffing gaps.

### Import and export

Range CSV imports remain available for migration from the original prototype.
Imports are validated as a batch, skip exact duplicates, and reject conflicting
range IDs. The download action provides a consistent domain-level CSV backup.

## CSV fallback

Set `DATA_BACKEND=csv` (or omit it when no database variables are present) and
run the app with `uv run streamlit run app.py`. The fallback uses `data/` and
retains atomic writes, file locking, backups, stale-edit protection, merging,
and employee deactivation/permanent deletion behavior. The sample CSV roster
uses the same DMC hierarchy and keeps the parent-level sample employee
unassigned until a team is chosen.

## Docker Compose

Copy `.env.example` to `.env`, fill in the password, and run:

```sh
docker compose up --build -d
docker compose logs -f
```

Open **http://localhost:8501**. Stop it with `docker compose down`.

The Dockerfile copies the pinned uv binary, runs `uv sync --locked`, and never
installs dependencies with pip. Keep `.env` outside the image and host.

## Database objects

| Object | Purpose |
|---|---|
| `profiles` | Optional Supabase Auth profile and application role |
| `departments` | DMC, DMC Department, and teams, linked through `parent_department_id` |
| `employees` | Current employee identity, team/department assignment, active state |
| `team_memberships` | Historical department or team assignments |
| `leave_types` | Annual, unpaid, and training leave categories |
| `leave_balances` | Per-employee yearly entitlement and adjustments |
| `availability_windows` | Dates during which vacation may be taken |
| `leave_requests` | Planned, approved, or cancelled inclusive date ranges |
| `leave_request_events` | Append-only request lifecycle events |
| `holidays` | Non-working dates and optional region |
| `v_merged_leave_periods` | SQL view that merges overlapping/consecutive requests |
| `v_department_coverage` | SQL view for workday staffing review |

## Project structure

| Component | Responsibility |
|---|---|
| `app.py` | Streamlit pages, forms, employee lifecycle controls, calendar |
| `planner/domain.py` | Validation, interval merging, workdays and coverage |
| `planner/repository.py` | CSV fallback persistence |
| `planner/sql_repository.py` | Supabase/Postgres persistence and URL handling |
| `supabase/migrations/` | Database schema, seeds, RLS, triggers, and views |
| `scripts/test_connection.py` | Safe connection/schema smoke test |
| `tests/` | Domain, CSV storage, and Streamlit form tests |

