package sandbox

import (
	"context"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/ids"
	"github.com/personal-agent/vm-manager/internal/platformfile"
	"github.com/personal-agent/vm-manager/internal/store"
)

// Scout's browser (a Max-plan feature): a headless browser inside the user's own container.
//
// The switch is one flag per sandbox (sandboxes.browser_enabled). It decides three things, and
// all three change together when the container is woken:
//   - the container's limits: more memory and more processes (fixed when a container is
//     created, so a changed switch re-creates the container on the next wake; /data is kept);
//   - platform.json: `browser_enabled`, which tells the Bridge to offer the browser;
//   - the egress rule: that container, and only it, may reach any public web address on ports
//     80 and 443.
// A running container keeps what it was woken with. The label LabelBrowser says what that is.

// MaxBrowserReportSeconds bounds one browser report from a container (a day). The time is
// measured inside the user's own container, so a report is taken as given up to this.
const MaxBrowserReportSeconds = 24 * 60 * 60

// hasBrowser reports whether the container was created with Scout's browser turned on.
func hasBrowser(c docker.Container) bool { return c.Labels[LabelBrowser] == "true" }

// egressRule is what goes into the user's Squid file for this container: the user's domains
// (plus the Claude Code hosts when needed), and the open web when the container was created
// with the browser. It follows the container, not the stored switch: the network opens and
// closes together with the browser itself.
func (m *Manager) egressRule(sb store.Sandbox, c docker.Container) egress.Rule {
	return egress.Rule{Domains: m.egressDomains(sb), OpenWeb: hasBrowser(c)}
}

// syncPlatformFileLocked loads platform.json (checking it against the stored hashes and
// caching the Bridge secret) and rewrites it when its browser switch differs from the
// sandbox's. Called on every wake, before the container starts.
func (m *Manager) syncPlatformFileLocked(ctx context.Context, sb store.Sandbox) error {
	f, err := m.loadPlatformFile(ctx, sb.UserID, sb.ContainerID, sb.MCPTokenHash, sb.BridgeSecretHash)
	if err != nil {
		return err
	}
	if f.BrowserEnabled == sb.BrowserEnabled {
		return nil
	}
	f.BrowserEnabled = sb.BrowserEnabled
	return platformfile.Write(ctx, m.eng, sb.ContainerID, m.cfg.AgentUID, f)
}

// SetBrowser turns Scout's browser on or off for the user. It only stores the switch: the
// container gets it on its next wake (re-created then if its limits must change). A sandbox
// that is awake keeps running as it is until it stops; the caller may stop it to apply the
// change sooner.
func (m *Manager) SetBrowser(ctx context.Context, userID string, enabled bool) (store.Sandbox, error) {
	if err := validateUserID(userID); err != nil {
		return store.Sandbox{}, err
	}
	l := m.userLock(userID)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := detached(ctx, time.Minute)
	defer cancel()
	sb, err := m.getSandbox(ctx, userID)
	if err != nil {
		return store.Sandbox{}, err
	}
	if sb.BrowserEnabled != enabled {
		if err := m.store.SetBrowserEnabled(ctx, userID, enabled, m.clk.Now()); err != nil {
			return store.Sandbox{}, err
		}
		m.log.Info("browser switch set; applied on the next wake", "user_id", userID, "browser", enabled,
			"state", string(sb.State))
	}
	return m.getSandbox(ctx, userID)
}

// recordBrowser stores the browser time the Bridge reported with the end of a turn. The report
// id makes it idempotent: the same report (a retried delivery, two listeners on one turn) is
// stored once. Nothing is stored when the browser never started (0 seconds).
func (m *Manager) recordBrowser(ctx context.Context, userID, deliveryID string, ev bridge.Event) {
	seconds := ev.BrowserSeconds
	if seconds <= 0 {
		return
	}
	if seconds > MaxBrowserReportSeconds {
		m.log.Warn("browser report above the limit; capped", "user_id", userID, "delivery_id", deliveryID,
			"reported", seconds, "limit", MaxBrowserReportSeconds)
		seconds = MaxBrowserReportSeconds
	}
	reportID := ev.BrowserReportID
	if reportID == "" {
		reportID = deliveryID
	}
	if len(reportID) > 300 {
		reportID = ids.Hash(reportID)
	}
	stored, err := m.store.AddBrowserSeconds(ctx, userID, reportID, seconds, m.clk.Now())
	if err != nil {
		m.log.Error("could not record browser time", "user_id", userID, "delivery_id", deliveryID,
			"seconds", seconds, "err", err, "alert", true)
		return
	}
	m.log.Info("browser time recorded", "user_id", userID, "delivery_id", deliveryID, "seconds", seconds, "new", stored)
}
