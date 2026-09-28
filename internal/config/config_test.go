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
	if c.IdleTimeout != 3*time.Minute || c.DeliverTimeout != 15*time.Minute || c.ShutdownGrace != 20*time.Second {
		t.Errorf("unexpected durations: %+v", c)
	}
	if c.AgentImageVersion != c.AgentImage {
		t.Errorf("image version should default to the image: %q vs %q", c.AgentImageVersion, c.AgentImage)
	}
}

func TestLoadOverrides(t *testing.T) {
	e := validEnv()
	e["AWAKE_CAP"] = "7"
	e["SANDBOX_CPUS"] = "0.5"
	e["IDLE_TIMEOUT"] = "90s"
	e["SANDBOX_VOLUME_OPTS"] = "type=xfs, o=size=2g"
	e["AGENT_IMAGE_VERSION"] = "v42"
	c, err := Load(env(e))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.AwakeCap != 7 || c.NanoCPUs != 5e8 || c.IdleTimeout != 90*time.Second || c.AgentImageVersion != "v42" {
		t.Errorf("overrides not applied: %+v", c)
	}
	if c.VolumeOpts["type"] != "xfs" || c.VolumeOpts["o"] != "size=2g" {
		t.Errorf("volume opts: %v", c.VolumeOpts)
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
