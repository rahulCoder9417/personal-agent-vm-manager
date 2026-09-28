// Package docker is the only code that talks to the Docker Engine.
// Engine is a small interface over the SDK so the manager can be unit-tested
// with Fake; Client is the real implementation.
package docker

import (
	"context"
	"errors"
	"io"
	"time"
)

// ErrNotFound means the container or volume does not exist.
var ErrNotFound = errors.New("docker: not found")

// ContainerSpec is everything needed to create one user's sandbox container.
type ContainerSpec struct {
	Name           string
	Image          string
	Env            []string
	Labels         map[string]string
	Runtime        string // "runsc" for gVisor
	MemoryBytes    int64
	NanoCPUs       int64
	PidsLimit      int64
	Network        string // the sandbox network, and nothing else
	VolumeName     string
	VolumeTarget   string // "/data"
	User           string // "uid:gid", never root
	ReadonlyRootfs bool
	Hostname       string
}

// Container is what the manager needs to know about a container.
type Container struct {
	ID      string
	Name    string
	Image   string
	Labels  map[string]string
	Running bool
	// IPs by network name.
	IPs map[string]string
}

// Engine is the Docker surface used by the vm-manager.
type Engine interface {
	// EnsureVolume creates the named volume if it does not exist.
	EnsureVolume(ctx context.Context, name, driver string, opts, labels map[string]string) error
	// RemoveVolume deletes a volume. Missing is not an error.
	RemoveVolume(ctx context.Context, name string) error
	// CreateContainer creates (does not start) a container and returns its id.
	CreateContainer(ctx context.Context, spec ContainerSpec) (string, error)
	StartContainer(ctx context.Context, id string) error
	// StopContainer sends SIGTERM and kills after timeout (timeout 0 = kill now). Stopped already is fine.
	StopContainer(ctx context.Context, id string, timeout time.Duration) error
	// RemoveContainer force-removes a container but never its volumes. Missing is not an error.
	RemoveContainer(ctx context.Context, id string) error
	// InspectContainer returns ErrNotFound when missing. id may be an id or a name.
	InspectContainer(ctx context.Context, id string) (Container, error)
	// ListContainers lists all containers (running or not) carrying every given label.
	ListContainers(ctx context.Context, labels map[string]string) ([]Container, error)
	// CopyTo extracts a tar stream into dir inside the container (works on stopped containers;
	// dir must be on a writable volume when the rootfs is read-only).
	CopyTo(ctx context.Context, id, dir string, tarStream io.Reader) error
	// CopyFrom returns a tar stream of path inside the container.
	CopyFrom(ctx context.Context, id, path string) (io.ReadCloser, error)
	// Signal sends a signal (e.g. "SIGHUP") to a running container.
	Signal(ctx context.Context, container, signal string) error
	// MemoryUsage is the container's current memory use in bytes (like `docker stats`).
	MemoryUsage(ctx context.Context, id string) (int64, error)
}
