-- Add the DMC organization hierarchy without rewriting the initial migration.
-- Existing department rows are kept so historical memberships remain valid.

BEGIN;

ALTER TABLE vacation.departments
    ADD COLUMN IF NOT EXISTS parent_department_id text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'departments_parent_department_fk'
          AND conrelid = 'vacation.departments'::regclass
    ) THEN
        ALTER TABLE vacation.departments
            ADD CONSTRAINT departments_parent_department_fk
            FOREIGN KEY (parent_department_id)
            REFERENCES vacation.departments(id)
            ON UPDATE CASCADE ON DELETE RESTRICT;
    END IF;
END
$$;

-- DMC is the top-level organization; DMC Department is its department.
UPDATE vacation.departments
SET name = 'DMC', parent_department_id = NULL, minimum_at_work = 0, active = true
WHERE id = 'DMC';

INSERT INTO vacation.departments (id, name, minimum_at_work, active, parent_department_id)
VALUES ('DMC_DEPARTMENT', 'DMC Department', 0, true, 'DMC')
ON CONFLICT (id) DO UPDATE
SET name = EXCLUDED.name,
    minimum_at_work = EXCLUDED.minimum_at_work,
    parent_department_id = EXCLUDED.parent_department_id,
    active = true;

-- Reuse the existing seeded department IDs for the new child teams so existing
-- employee references and historical membership references stay intact.
UPDATE vacation.departments
SET name = 'Data Analytics', minimum_at_work = 2,
    parent_department_id = 'DMC_DEPARTMENT', active = true
WHERE id = 'DATA_ANALYTICS_ENGINEERING';

UPDATE vacation.departments
SET name = 'Data Governance', minimum_at_work = 1,
    parent_department_id = 'DMC_DEPARTMENT', active = true
WHERE id = 'DATA_GOVERNANCE';

UPDATE vacation.departments
SET name = 'ML', minimum_at_work = 1,
    parent_department_id = 'DMC_DEPARTMENT', active = true
WHERE id = 'ML';

-- Move old sample/current employee assignments to the matching team. Employees
-- that belonged directly to DMC remain at department level until assigned to a team.
UPDATE vacation.employees
SET department_id = CASE department_id
    WHEN 'DMC' THEN 'DMC_DEPARTMENT'
    ELSE department_id
END
WHERE department_id IN ('DMC', 'DATA_ANALYTICS_ENGINEERING', 'DATA_GOVERNANCE', 'ML');

-- Other legacy department rows and their membership history remain in place but
-- are retired from the active roster. Their employees display as unassigned.
UPDATE vacation.departments
SET active = false
WHERE id NOT IN ('DMC', 'DMC_DEPARTMENT', 'DATA_ANALYTICS_ENGINEERING', 'DATA_GOVERNANCE', 'ML');

CREATE INDEX IF NOT EXISTS departments_parent_active_idx
    ON vacation.departments(parent_department_id, active);

-- Staffing coverage is reported for the three teams under DMC Department.
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
      AND d.parent_department_id = 'DMC_DEPARTMENT'
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

COMMIT;
