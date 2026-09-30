package platformfile

import (
	"context"
	"testing"

	"github.com/personal-agent/vm-manager/internal/docker"
)

func TestWriteReadRoundTrip(t *testing.T) {
	ctx := context.Background()
	f := docker.NewFake()
	id, err := f.CreateContainer(ctx, docker.ContainerSpec{Name: "c", VolumeName: "v", VolumeTarget: "/data"})
	if err != nil {
		t.Fatal(err)
	}
	in := File{UserID: "u1", MCPToken: "tok", BridgeSecret: "sec", LiteLLMKey: "sk-1", Timezone: "Europe/Berlin", Runtime: "claude_code",
		MCPServers: []MCPServer{{ID: "m1", Name: "notes", URL: "https://mcp.example.com", AuthHeaderName: "X-Key", AuthSecret: "k"}}}
	if err := Write(ctx, f, id, 10001, in); err != nil {
		t.Fatal(err)
	}
	vol, _ := f.Volume("v")
	if vol.Modes[".platform/platform.json"] != 0o600 || vol.UIDs[".platform/platform.json"] != 10001 {
		t.Fatalf("mode/uid: %o %d", vol.Modes[".platform/platform.json"], vol.UIDs[".platform/platform.json"])
	}
	out, err := Read(ctx, f, id)
	if err != nil {
		t.Fatal(err)
	}
	if out.Version != Version || out.MCPToken != "tok" || out.BridgeSecret != "sec" || out.LiteLLMKey != "sk-1" ||
		len(out.MCPServers) != 1 || out.MCPServers[0].AuthSecret != "k" || out.Timezone != "Europe/Berlin" || out.Runtime != "claude_code" {
		t.Fatalf("round trip: %+v", out)
	}
	if err := Write(ctx, f, id, 10001, File{UserID: "u1"}); err == nil {
		t.Fatal("missing secrets must be rejected")
	}
}
