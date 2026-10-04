-- Scout's browser (a Max-plan feature): a headless browser inside the user's own container.
-- browser_enabled is the switch the backend sets (CreateSandbox, SetBrowser). It takes effect on the
-- user's next wake: the container is re-created then if its limits must change.
ALTER TABLE sandboxes
    ADD COLUMN browser_enabled boolean NOT NULL DEFAULT false;

-- Browser time for credits: one row per report from a container (the Bridge reports the seconds the
-- browser was open with the end of a turn). report_id is the Bridge's id for that report, so a repeated
-- report is stored once. GetUsage sums the rows whose recorded_at falls inside the asked window, so
-- back-to-back windows count every second exactly once.
-- No foreign key on purpose: rows outlive DeleteSandbox so the last use can still be charged.
-- awake_periods.browser_seconds (migration 0001) was never written and is no longer read.
CREATE TABLE browser_usage (
    id          bigserial   PRIMARY KEY,
    user_id     text        NOT NULL,
    report_id   text        NOT NULL,
    seconds     bigint      NOT NULL CHECK (seconds > 0),
    recorded_at timestamptz NOT NULL
);

CREATE UNIQUE INDEX browser_usage_report_key ON browser_usage (user_id, report_id);
CREATE INDEX browser_usage_user_recorded ON browser_usage (user_id, recorded_at);
CREATE INDEX browser_usage_recorded ON browser_usage (recorded_at);
