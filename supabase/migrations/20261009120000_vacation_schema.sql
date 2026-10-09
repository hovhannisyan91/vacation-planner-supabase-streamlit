-- Vacation planner schema for Supabase/PostgreSQL.
-- Run this file once in Supabase Dashboard -> SQL Editor as the postgres role.
-- The Streamlit app connects to this private schema through SQLAlchemy.
-- No API keys or database passwords belong in this file.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vacation_app') THEN
        CREATE ROLE vacation_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS vacation AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA vacation FROM PUBLIC, anon, authenticated, service_role;
GRANT USAGE ON SCHEMA vacation TO vacation_app;
GRANT CONNECT ON DATABASE postgres TO vacation_app;

CREATE TABLE IF NOT EXISTS vacation.profiles (
    user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    display_name text NOT NULL DEFAULT '' CHECK (length(btrim(display_name)) <= 160),
    role text NOT NULL DEFAULT 'planner'
        CHECK (role IN ('employee', 'manager', 'planner', 'admin')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vacation.departments (
    id text PRIMARY KEY DEFAULT (gen_random_uuid()::text)
        CHECK (length(btrim(id)) > 0),
    name text NOT NULL UNIQUE CHECK (length(btrim(name)) > 0),
    minimum_at_work integer NOT NULL DEFAULT 1 CHECK (minimum_at_work >= 0),
    active boolean NOT NULL DEFAULT true,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vacation.employees (
    employee_id text PRIMARY KEY CHECK (length(btrim(employee_id)) > 0),
    employee_code text UNIQUE CHECK (employee_code IS NULL OR length(btrim(employee_code)) > 0),
    full_name text NOT NULL CHECK (length(btrim(full_name)) > 0),
    auth_user_id uuid UNIQUE REFERENCES auth.users(id) ON DELETE SET NULL,
    manager_id text REFERENCES vacation.employees(employee_id) ON DELETE SET NULL,
    department_id text NOT NULL REFERENCES vacation.departments(id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    active boolean NOT NULL DEFAULT true,
    hire_date date,
    termination_date date,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT employee_dates_valid CHECK (
        termination_date IS NULL OR hire_date IS NULL OR termination_date >= hire_date
    )
);

CREATE TABLE IF NOT EXISTS vacation.team_memberships (
    employee_id text NOT NULL REFERENCES vacation.employees(employee_id) ON DELETE RESTRICT,
    department_id text NOT NULL REFERENCES vacation.departments(id) ON DELETE RESTRICT,
    valid_from date NOT NULL,
    valid_to date,
    primary_department boolean NOT NULL DEFAULT true,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (employee_id, department_id, valid_from),
    CONSTRAINT membership_dates_valid CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS vacation.leave_types (
    id text PRIMARY KEY CHECK (length(btrim(id)) > 0),
    code text NOT NULL UNIQUE CHECK (length(btrim(code)) > 0),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    requires_availability boolean NOT NULL DEFAULT true,
    counts_against_balance boolean NOT NULL DEFAULT true,
    active boolean NOT NULL DEFAULT true,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vacation.leave_balances (
    employee_id text NOT NULL REFERENCES vacation.employees(employee_id) ON DELETE RESTRICT,
    leave_type_id text NOT NULL REFERENCES vacation.leave_types(id) ON DELETE RESTRICT,
    leave_year integer NOT NULL CHECK (leave_year BETWEEN 2000 AND 2200),
    entitlement_days numeric(7,2) NOT NULL DEFAULT 0 CHECK (entitlement_days >= 0),
    carried_days numeric(7,2) NOT NULL DEFAULT 0 CHECK (carried_days >= 0),
    adjustment_days numeric(7,2) NOT NULL DEFAULT 0,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (employee_id, leave_type_id, leave_year)
);

CREATE TABLE IF NOT EXISTS vacation.availability_windows (
    id text PRIMARY KEY DEFAULT (gen_random_uuid()::text)
        CHECK (length(btrim(id)) > 0),
    employee_id text NOT NULL REFERENCES vacation.employees(employee_id) ON DELETE RESTRICT,
    start_date date NOT NULL,
    end_date date NOT NULL,
    note text NOT NULL DEFAULT '',
    active boolean NOT NULL DEFAULT true,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT availability_dates_valid CHECK (
        isfinite(start_date) AND isfinite(end_date) AND end_date >= start_date
    )
);

CREATE TABLE IF NOT EXISTS vacation.leave_requests (
    id text PRIMARY KEY DEFAULT (gen_random_uuid()::text)
        CHECK (length(btrim(id)) > 0),
    employee_id text NOT NULL REFERENCES vacation.employees(employee_id) ON DELETE RESTRICT,
    leave_type_id text NOT NULL REFERENCES vacation.leave_types(id) ON DELETE RESTRICT,
    start_date date NOT NULL,
    end_date date NOT NULL,
    status text NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'approved', 'cancelled')),
    reason text NOT NULL DEFAULT '',
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT leave_dates_valid CHECK (
        isfinite(start_date) AND isfinite(end_date) AND end_date >= start_date
    )
);

CREATE TABLE IF NOT EXISTS vacation.leave_request_events (
    id text PRIMARY KEY DEFAULT (gen_random_uuid()::text)
        CHECK (length(btrim(id)) > 0),
    leave_request_id text NOT NULL REFERENCES vacation.leave_requests(id) ON DELETE CASCADE,
    actor_user_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    event_type text NOT NULL CHECK (length(btrim(event_type)) > 0),
    comment text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vacation.holidays (
    date date PRIMARY KEY CHECK (isfinite(date)),
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    region text NOT NULL DEFAULT '',
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS employees_department_idx
    ON vacation.employees(department_id, active);
CREATE INDEX IF NOT EXISTS memberships_employee_dates_idx
    ON vacation.team_memberships(employee_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS availability_employee_dates_idx
    ON vacation.availability_windows(employee_id, start_date, end_date)
    WHERE active;
CREATE INDEX IF NOT EXISTS leave_requests_employee_dates_idx
    ON vacation.leave_requests(employee_id, status, start_date, end_date);
CREATE INDEX IF NOT EXISTS leave_request_events_request_idx
    ON vacation.leave_request_events(leave_request_id, created_at);

CREATE OR REPLACE FUNCTION vacation.stamp_versioned_record()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.version := COALESCE(NEW.version, 1);
    ELSE
        NEW.version := OLD.version + 1;
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION vacation.stamp_profile()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS stamp_departments ON vacation.departments;
CREATE TRIGGER stamp_departments BEFORE INSERT OR UPDATE ON vacation.departments
    FOR EACH ROW EXECUTE FUNCTION vacation.stamp_versioned_record();
DROP TRIGGER IF EXISTS stamp_employees ON vacation.employees;
CREATE TRIGGER stamp_employees BEFORE INSERT OR UPDATE ON vacation.employees
    FOR EACH ROW EXECUTE FUNCTION vacation.stamp_versioned_record();
DROP TRIGGER IF EXISTS stamp_memberships ON vacation.team_memberships;
CREATE TRIGGER stamp_memberships BEFORE INSERT OR UPDATE ON vacation.team_memberships
    FOR EACH ROW EXECUTE FUNCTION vacation.stamp_versioned_record();
DROP TRIGGER IF EXISTS stamp_leave_types ON vacation.leave_types;
CREATE TRIGGER stamp_leave_types BEFORE INSERT OR UPDATE ON vacation.leave_types
    FOR EACH ROW EXECUTE FUNCTION vacation.stamp_versioned_record();
DROP TRIGGER IF EXISTS stamp_leave_balances ON vacation.leave_balances;
CREATE TRIGGER stamp_leave_balances BEFORE INSERT OR UPDATE ON vacation.leave_balances
    FOR EACH ROW EXECUTE FUNCTION vacation.stamp_versioned_record();
DROP TRIGGER IF EXISTS stamp_availability_windows ON vacation.availability_windows;
CREATE TRIGGER stamp_availability_windows BEFORE INSERT OR UPDATE ON vacation.availability_windows
    FOR EACH ROW EXECUTE FUNCTION vacation.stamp_versioned_record();
DROP TRIGGER IF EXISTS stamp_leave_requests ON vacation.leave_requests;
CREATE TRIGGER stamp_leave_requests BEFORE INSERT OR UPDATE ON vacation.leave_requests
    FOR EACH ROW EXECUTE FUNCTION vacation.stamp_versioned_record();
DROP TRIGGER IF EXISTS stamp_holidays ON vacation.holidays;
CREATE TRIGGER stamp_holidays BEFORE INSERT OR UPDATE ON vacation.holidays
    FOR EACH ROW EXECUTE FUNCTION vacation.stamp_versioned_record();
DROP TRIGGER IF EXISTS stamp_profiles ON vacation.profiles;
CREATE TRIGGER stamp_profiles BEFORE INSERT OR UPDATE ON vacation.profiles
    FOR EACH ROW EXECUTE FUNCTION vacation.stamp_profile();

INSERT INTO vacation.departments (id, name, minimum_at_work)
VALUES
    ('DMC', 'DMC', 1),
    ('DATA_ANALYTICS_ENGINEERING', 'Data Analytics and Engineering', 1),
    ('DATA_GOVERNANCE', 'Data Governance', 1),
    ('ML', 'ML', 1)
ON CONFLICT (name) DO UPDATE SET active = true;

INSERT INTO vacation.leave_types (id, code, name, requires_availability, counts_against_balance)
VALUES
    ('annual', 'annual', 'Annual leave', true, true),
    ('unpaid', 'unpaid', 'Unpaid leave', false, false),
    ('training', 'training', 'Training', false, false)
ON CONFLICT (id) DO UPDATE SET active = true;

-- Database clients should use the private schema through the trusted backend role.
REVOKE ALL ON ALL TABLES IN SCHEMA vacation FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA vacation FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA vacation FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA vacation TO vacation_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vacation TO vacation_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA vacation TO vacation_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA vacation
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vacation_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA vacation
    GRANT USAGE, SELECT ON SEQUENCES TO vacation_app;

ALTER TABLE vacation.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE vacation.departments ENABLE ROW LEVEL SECURITY;
ALTER TABLE vacation.employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE vacation.team_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE vacation.leave_types ENABLE ROW LEVEL SECURITY;
ALTER TABLE vacation.leave_balances ENABLE ROW LEVEL SECURITY;
ALTER TABLE vacation.availability_windows ENABLE ROW LEVEL SECURITY;
ALTER TABLE vacation.leave_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE vacation.leave_request_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE vacation.holidays ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS vacation_app_access_profiles ON vacation.profiles;
CREATE POLICY vacation_app_access_profiles ON vacation.profiles FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS vacation_app_access_departments ON vacation.departments;
CREATE POLICY vacation_app_access_departments ON vacation.departments FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS vacation_app_access_employees ON vacation.employees;
CREATE POLICY vacation_app_access_employees ON vacation.employees FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS vacation_app_access_memberships ON vacation.team_memberships;
CREATE POLICY vacation_app_access_memberships ON vacation.team_memberships FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS vacation_app_access_leave_types ON vacation.leave_types;
CREATE POLICY vacation_app_access_leave_types ON vacation.leave_types FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS vacation_app_access_leave_balances ON vacation.leave_balances;
CREATE POLICY vacation_app_access_leave_balances ON vacation.leave_balances FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS vacation_app_access_availability ON vacation.availability_windows;
CREATE POLICY vacation_app_access_availability ON vacation.availability_windows FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS vacation_app_access_leave_requests ON vacation.leave_requests;
CREATE POLICY vacation_app_access_leave_requests ON vacation.leave_requests FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS vacation_app_access_events ON vacation.leave_request_events;
CREATE POLICY vacation_app_access_events ON vacation.leave_request_events FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS vacation_app_access_holidays ON vacation.holidays;
CREATE POLICY vacation_app_access_holidays ON vacation.holidays FOR ALL TO vacation_app
    USING (true) WITH CHECK (true);

-- Overlapping and consecutive requests remain intact; this view exposes merged islands.
CREATE OR REPLACE VIEW vacation.v_merged_leave_periods AS
WITH ordered AS (
    SELECT
        lr.*,
        max(lr.end_date) OVER (
            PARTITION BY lr.employee_id, lr.status
            ORDER BY lr.start_date, lr.end_date, lr.id
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS previous_max_end
    FROM vacation.leave_requests AS lr
    WHERE lr.status <> 'cancelled'
), marked AS (
    SELECT ordered.*,
        CASE
            WHEN previous_max_end IS NULL OR start_date > previous_max_end + 1
            THEN 1 ELSE 0
        END AS starts_group
    FROM ordered
), grouped AS (
    SELECT marked.*,
        sum(starts_group) OVER (
            PARTITION BY employee_id, status
            ORDER BY start_date, end_date, id
            ROWS UNBOUNDED PRECEDING
        ) AS group_number
    FROM marked
)
SELECT
    employee_id,
    status,
    min(start_date) AS start_date,
    max(end_date) AS end_date,
    array_agg(id ORDER BY start_date, end_date, id) AS source_ids
FROM grouped
GROUP BY employee_id, status, group_number;

-- One row per workday/department for coverage review. Holidays are excluded.
CREATE OR REPLACE VIEW vacation.v_department_coverage AS
WITH workdays AS (
    SELECT day::date AS day
    FROM generate_series(current_date - interval '365 days', current_date + interval '730 days', interval '1 day') AS day
    WHERE extract(isodow FROM day) < 6
      AND NOT EXISTS (SELECT 1 FROM vacation.holidays h WHERE h.date = day::date)
), roster AS (
    SELECT d.id AS department_id, d.name AS department, d.minimum_at_work,
           e.employee_id, e.full_name
    FROM vacation.departments d
    JOIN vacation.employees e ON e.department_id = d.id
    WHERE d.active AND e.active
), absences AS (
    SELECT employee_id, start_date, end_date
    FROM vacation.leave_requests
    WHERE status IN ('planned', 'approved')
)
SELECT
    w.day,
    r.department_id,
    r.department,
    r.minimum_at_work,
    count(r.employee_id)::integer AS rostered,
    count(r.employee_id) FILTER (
        WHERE NOT EXISTS (
            SELECT 1 FROM absences a
            WHERE a.employee_id = r.employee_id
              AND w.day BETWEEN a.start_date AND a.end_date
        )
    )::integer AS present
FROM workdays w
JOIN roster r ON true
GROUP BY w.day, r.department_id, r.department, r.minimum_at_work;

CREATE OR REPLACE VIEW vacation.v_team_coverage AS
SELECT * FROM vacation.v_department_coverage;

GRANT SELECT ON vacation.v_merged_leave_periods, vacation.v_department_coverage, vacation.v_team_coverage
    TO vacation_app;

COMMIT;

-- After this migration, either keep using the postgres role for the first local prototype,
-- or set a long random password and LOGIN on vacation_app, then use that least-privilege role.
-- Example (run separately, replacing the placeholder locally):
-- ALTER ROLE vacation_app LOGIN PASSWORD '<LONG_RANDOM_PASSWORD>';
