package docker

import (
	"archive/tar"
	"bytes"
	"context"
	"io"
	"slices"
	"testing"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/mount"
)

func testSpec() ContainerSpec {
	return ContainerSpec{
		Name: "pa-sbx-u1", Image: "agent:v1", Env: []string{"USER_ID=u1"},
		Labels: map[string]string{"pa.managed": "true"}, Runtime: "runsc",
		MemoryBytes: 600 << 20, NanoCPUs: 1e9, PidsLimit: 256, Network: "sandbox",
		VolumeName: "pa-data-u1", VolumeTarget: "/data", User: "10001:10001", ReadonlyRootfs: true, Hostname: "agent",
	}
}

func TestBuildConfigsSecurity(t *testing.T) {
	cfg, host, net := BuildConfigs(testSpec())
	if host.Runtime != "runsc" {
		t.Errorf("runtime = %q, want runsc (gVisor)", host.Runtime)
	}
	if !host.ReadonlyRootfs {
		t.Error("rootfs must be read-only")
	}
	if !slices.Equal(host.CapDrop, []string{"ALL"}) || len(host.CapAdd) != 0 {
		t.Errorf("caps: drop=%v add=%v", host.CapDrop, host.CapAdd)
	}
	if !slices.Contains(host.SecurityOpt, "no-new-privileges:true") {
		t.Errorf("security opts: %v", host.SecurityOpt)
	}
	if host.Privileged {
		t.Error("must not be privileged")
	}
	if host.Memory != 600<<20 || host.MemorySwap != host.Memory || host.NanoCPUs != 1e9 || host.PidsLimit == nil || *host.PidsLimit != 256 {
		t.Errorf("resources: %+v", host.Resources)
	}
	if string(host.NetworkMode) != "sandbox" || len(net.EndpointsConfig) != 1 || net.EndpointsConfig["sandbox"] == nil {
		t.Errorf("network: mode=%s endpoints=%v", host.NetworkMode, net.EndpointsConfig)
	}
	if len(host.PortBindings) != 0 || host.PublishAllPorts {
		t.Error("no host ports may be published")
	}
	if len(host.Binds) != 0 || len(host.Mounts) != 1 || host.Mounts[0].Type != mount.TypeVolume ||
		host.Mounts[0].Source != "pa-data-u1" || host.Mounts[0].Target != "/data" || host.Mounts[0].ReadOnly {
		t.Errorf("mounts: binds=%v mounts=%+v", host.Binds, host.Mounts)
	}
	if host.RestartPolicy.Name != container.RestartPolicyDisabled {
		t.Errorf("restart policy: %v", host.RestartPolicy)
	}
	if cfg.User != "10001:10001" || cfg.Image != "agent:v1" || cfg.Labels["pa.managed"] != "true" {
		t.Errorf("config: %+v", cfg)
	}
}

func TestMemoryFromStats(t *testing.T) {
	v2 := container.MemoryStats{Usage: 500, Stats: map[string]uint64{"inactive_file": 100}}
	if MemoryFromStats(v2) != 400 {
		t.Error("cgroup v2 calc")
	}
	v1 := container.MemoryStats{Usage: 500, Stats: map[string]uint64{"total_inactive_file": 50}}
	if MemoryFromStats(v1) != 450 {
		t.Error("cgroup v1 calc")
	}
}

func TestFakeCopyRoundTripAndReadOnlyRootfs(t *testing.T) {
	ctx := context.Background()
	f := NewFake()
	id, err := f.CreateContainer(ctx, testSpec())
	if err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	_ = tw.WriteHeader(&tar.Header{Name: ".platform/x.json", Mode: 0o600, Size: 2, Typeflag: tar.TypeReg, Uid: 10001})
	_, _ = tw.Write([]byte("{}"))
	_ = tw.Close()
	if err := f.CopyTo(ctx, id, "/data", bytes.NewReader(buf.Bytes())); err != nil {
		t.Fatal(err)
	}
	if err := f.CopyTo(ctx, id, "/etc", bytes.NewReader(buf.Bytes())); err == nil {
		t.Fatal("writing outside /data must fail (read-only rootfs)")
	}
	rc, err := f.CopyFrom(ctx, id, "/data/.platform/x.json")
	if err != nil {
		t.Fatal(err)
	}
	tr := tar.NewReader(rc)
	h, err := tr.Next()
	if err != nil || h.Name != "x.json" {
		t.Fatalf("entry: %v %v", h, err)
	}
	body, _ := io.ReadAll(tr)
	if string(body) != "{}" {
		t.Fatalf("body %q", body)
	}
}
