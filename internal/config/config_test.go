package config

import (
	"strings"
	"testing"
	"time"
)

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func validEnv() map[string]string {
	return map[string]string{
		"VM_INTERNAL_SECRET": strings.Repeat("s", 32),
		"DATABASE_URL":       "postgres://u:p@localhost:5432/vm",
	}
}

func TestLoadDefaults(t *testing.T) {
	c, err := Load(env(validEnv()))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.GRPCAddr != ":9090" || c.SandboxNetwork != "personal-agent_sandbox" || c.Runtime != "runsc" {
		t.Errorf("unexpected defaults: %+v", c)
	}
	if c.EgressProxyURL != "http://172.30.0.2:3128" || c.EgressAllowlistDir != "/allowlists" || c.EgressProxyContainer != "egress-proxy" {
		t.Errorf("unexpected egress defaults: %+v", c)
	}
	if c.LiteLLMBaseURL != "http://litellm:4000" || c.PlatformMCPURL != "http://api:8080/mcp" {
		t.Errorf("unexpected url defaults: %+v", c)
	}
	if c.MemoryBytes != 600*1024*1024 || c.NanoCPUs != 1e9 || c.PidsLimit != 256 {
		t.Errorf("unexpected limits: mem=%d cpu=%d pids=%d", c.MemoryBytes, c.NanoCPUs, c.PidsLimit)
	}
	if c.BrowserMemoryBytes != 2048*1024*1024 || c.BrowserPidsLimit != 1024 {
		t.Errorf("unexpected browser limits: mem=%d pids=%d", c.BrowserMemoryBytes, c.BrowserPidsLimit)
	}
	if c.IdleTimeout != 3*time.Minute || c.DeliverTimeout != 15*time.Minute || c.ShutdownGrace != 20*time.Second {
		t.Errorf("unexpected durations: %+v", c)
	}
	if c.AgentImageVersion != c.AgentImage {
		t.Errorf("image version should default to the image: %q vs %q", c.AgentImageVersion, c.AgentImage)
	}
	if strings.Join(c.ClaudeEgressDomains, ",") != "api.anthropic.com,claude.ai,claude.com,platform.claude.com" ||
		c.ClaudeLoginTTL != 10*time.Minute {
		t.Errorf("unexpected claude defaults: %v %v", c.ClaudeEgressDomains, c.ClaudeLoginTTL)
	}
}

func TestLoadOverrides(t *testing.T) {
	e := validEnv()
	e["AWAKE_CAP"] = "7"
	e["SANDBOX_CPUS"] = "0.5"
	e["IDLE_TIMEOUT"] = "90s"
	e["SANDBOX_VOLUME_OPTS"] = "type=xfs, o=size=2g"
	e["AGENT_IMAGE_VERSION"] = "v42"
	e["CLAUDE_EGRESS_DOMAINS"] = " api.anthropic.com, *.claude.ai "
	e["SANDBOX_BROWSER_MEMORY_MB"] = "3072"
	e["SANDBOX_BROWSER_PIDS_LIMIT"] = "2048"
	c, err := Load(env(e))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.BrowserMemoryBytes != 3072*1024*1024 || c.BrowserPidsLimit != 2048 || c.MemoryBytes != 600*1024*1024 || c.PidsLimit != 256 {
		t.Errorf("browser limits: mem=%d pids=%d (others %d, %d)", c.BrowserMemoryBytes, c.BrowserPidsLimit, c.MemoryBytes, c.PidsLimit)
	}
	if c.AwakeCap != 7 || c.NanoCPUs != 5e8 || c.IdleTimeout != 90*time.Second || c.AgentImageVersion != "v42" {
		t.Errorf("overrides not applied: %+v", c)
	}
	if c.VolumeOpts["type"] != "xfs" || c.VolumeOpts["o"] != "size=2g" {
		t.Errorf("volume opts: %v", c.VolumeOpts)
	}
	if strings.Join(c.ClaudeEgressDomains, ",") != ".claude.ai,api.anthropic.com" {
		t.Errorf("claude egress domains: %v", c.ClaudeEgressDomains)
	}
}

func TestLoadRejectsBadValues(t *testing.T) {
	cases := map[string]map[string]string{
		"short secret": {"VM_INTERNAL_SECRET": "short"},
		"no db":        {"DATABASE_URL": ""},
		"bad url":      {"LITELLM_BASE_URL": "not a url"},
		"bad int":      {"AWAKE_CAP": "many"},
		"zero cap":     {"AWAKE_CAP": "0"},
		"bad dur":      {"IDLE_TIMEOUT": "3 minutes"},
		"relative dir": {"EGRESS_ALLOWLIST_DIR": "allowlists"},
		"root uid":     {"SANDBOX_AGENT_UID": "0"},
		"bad opts":     {"SANDBOX_VOLUME_OPTS": "novalue"},
		"bad domains":  {"CLAUDE_EGRESS_DOMAINS": "api.anthropic.com,10.0.0.1"},
		"zero ttl":     {"CLAUDE_LOGIN_TTL": "0s"},
		// A container with the browser never gets less than one without.
		"small browser memory": {"SANDBOX_BROWSER_MEMORY_MB": "512"},
		"browser memory below": {"SANDBOX_MEMORY_MB": "4096"},
		"small browser pids":   {"SANDBOX_BROWSER_PIDS_LIMIT": "100"},
		"bad browser memory":   {"SANDBOX_BROWSER_MEMORY_MB": "lots"},
	}
	for name, override := range cases {
		t.Run(name, func(t *testing.T) {
			e := validEnv()
			for k, v := range override {
				e[k] = v
			}
			if _, err := Load(env(e)); err == nil {
				t.Fatalf("expected an error")
			}
		})
	}
}
