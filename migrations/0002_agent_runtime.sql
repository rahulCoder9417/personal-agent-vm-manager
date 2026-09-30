-- Which agent answers in the sandbox: Hermes (platform models) or Claude Code (the user's own Claude plan).
-- claude_connected is the last status the vm-manager saw from the Bridge (Claude Code signed in or not);
-- the credentials themselves live only in the user's /data volume, written by the Claude Code CLI.
ALTER TABLE sandboxes
    ADD COLUMN runtime          text    NOT NULL DEFAULT 'hermes' CHECK (runtime IN ('hermes', 'claude_code')),
    ADD COLUMN claude_connected boolean NOT NULL DEFAULT false;
