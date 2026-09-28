// Package platformfile reads and writes /data/.platform/platform.json inside a
// user's volume. It is the ONLY place the plaintext per-container secrets live
// (the database keeps only hashes). Keeping them on the user's own volume means
// a container re-created for an image rollout reuses the same Platform MCP token,
// so the hash the backend stores never changes.
package platformfile

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/personal-agent/vm-manager/internal/docker"
)

const (
	// DataDir is where the user's volume is mounted.
	DataDir = "/data"
	// Path is the file's absolute path inside the container.
	Path = "/data/.platform/platform.json"
	// Version of the file format.
	Version = 1
)

// MCPServer is one of the user's own MCP servers.
type MCPServer struct {
	ID             string `json:"id"`
	Name           string `json:"name"`
	URL            string `json:"url"`
	AuthHeaderName string `json:"auth_header_name,omitempty"`
	AuthSecret     string `json:"auth_secret,omitempty"`
}

// File is the content of platform.json. The Bridge renders the Hermes config from it at every boot.
type File struct {
	Version      int         `json:"version"`
	UserID       string      `json:"user_id"`
	MCPToken     string      `json:"mcp_token"`
	BridgeSecret string      `json:"bridge_secret"`
	LiteLLMKey   string      `json:"litellm_key"`
	Timezone     string      `json:"timezone,omitempty"`
	MCPServers   []MCPServer `json:"mcp_servers"`
}

// Validate checks the required fields.
func (f File) Validate() error {
	if f.UserID == "" || f.MCPToken == "" || f.BridgeSecret == "" || f.LiteLLMKey == "" {
		return errors.New("platform file: missing required field")
	}
	return nil
}

// Write stores f in the container's /data volume (the container may be stopped).
// Files are 0600 / dirs 0700 and owned by uid (the non-root agent user).
func Write(ctx context.Context, eng docker.Engine, containerID string, uid int, f File) error {
	if err := f.Validate(); err != nil {
		return err
	}
	if f.Version == 0 {
		f.Version = Version
	}
	if f.MCPServers == nil {
		f.MCPServers = []MCPServer{}
	}
	body, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	now := time.Now()
	if err := tw.WriteHeader(&tar.Header{Name: ".platform/", Typeflag: tar.TypeDir, Mode: 0o700, Uid: uid, Gid: uid, ModTime: now}); err != nil {
		return err
	}
	if err := tw.WriteHeader(&tar.Header{Name: ".platform/platform.json", Typeflag: tar.TypeReg, Mode: 0o600,
		Size: int64(len(body)), Uid: uid, Gid: uid, ModTime: now}); err != nil {
		return err
	}
	if _, err := tw.Write(body); err != nil {
		return err
	}
	if err := tw.Close(); err != nil {
		return err
	}
	if err := eng.CopyTo(ctx, containerID, DataDir, &buf); err != nil {
		return fmt.Errorf("write platform file: %w", err)
	}
	return nil
}

// Read loads platform.json from the container's /data volume.
func Read(ctx context.Context, eng docker.Engine, containerID string) (File, error) {
	rc, err := eng.CopyFrom(ctx, containerID, Path)
	if err != nil {
		return File{}, fmt.Errorf("read platform file: %w", err)
	}
	defer rc.Close()
	tr := tar.NewReader(rc)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			return File{}, errors.New("read platform file: not in archive")
		}
		if err != nil {
			return File{}, fmt.Errorf("read platform file: %w", err)
		}
		if h.Typeflag != tar.TypeReg {
			continue
		}
		var f File
		if err := json.NewDecoder(io.LimitReader(tr, 1<<20)).Decode(&f); err != nil {
			return File{}, fmt.Errorf("decode platform file: %w", err)
		}
		if err := f.Validate(); err != nil {
			return File{}, err
		}
		return f, nil
	}
}
