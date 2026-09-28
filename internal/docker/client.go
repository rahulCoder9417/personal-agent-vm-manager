package docker

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/api/types/mount"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/api/types/volume"
	"github.com/docker/docker/client"
)

// Client is the real Engine backed by the Docker Engine SDK.
// It reads DOCKER_HOST etc. from the environment.
type Client struct {
	api *client.Client
}

// NewClient connects to the Docker daemon and checks it answers.
func NewClient(ctx context.Context) (*Client, error) {
	api, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return nil, fmt.Errorf("docker client: %w", err)
	}
	if _, err := api.Ping(ctx); err != nil {
		_ = api.Close()
		return nil, fmt.Errorf("docker ping: %w", err)
	}
	return &Client{api: api}, nil
}

// Close releases the SDK client.
func (c *Client) Close() error { return c.api.Close() }

func notFound(err error) error {
	if cerrdefs.IsNotFound(err) {
		return ErrNotFound
	}
	return err
}

// BuildConfigs turns a ContainerSpec into the SDK's create arguments.
// Pure function so the security settings can be unit-tested.
func BuildConfigs(spec ContainerSpec) (*container.Config, *container.HostConfig, *network.NetworkingConfig) {
	pids := spec.PidsLimit
	init := true
	cfg := &container.Config{
		Image:    spec.Image,
		Env:      append([]string(nil), spec.Env...),
		Labels:   spec.Labels,
		User:     spec.User,
		Hostname: spec.Hostname,
	}
	host := &container.HostConfig{
		Runtime:        spec.Runtime,
		ReadonlyRootfs: spec.ReadonlyRootfs,
		CapDrop:        []string{"ALL"},
		SecurityOpt:    []string{"no-new-privileges:true"},
		Privileged:     false,
		NetworkMode:    container.NetworkMode(spec.Network),
		RestartPolicy:  container.RestartPolicy{Name: container.RestartPolicyDisabled},
		Init:           &init, // tini as PID 1 reaps MCP/terminal subprocesses
		Mounts: []mount.Mount{{
			Type:   mount.TypeVolume,
			Source: spec.VolumeName,
			Target: spec.VolumeTarget,
		}},
		Resources: container.Resources{
			Memory:     spec.MemoryBytes,
			MemorySwap: spec.MemoryBytes, // no swap on top of the memory limit
			NanoCPUs:   spec.NanoCPUs,
			PidsLimit:  &pids,
		},
		LogConfig: container.LogConfig{Type: "json-file", Config: map[string]string{"max-size": "10m", "max-file": "3"}},
	}
	net := &network.NetworkingConfig{EndpointsConfig: map[string]*network.EndpointSettings{spec.Network: {}}}
	return cfg, host, net
}

// EnsureVolume implements Engine.
func (c *Client) EnsureVolume(ctx context.Context, name, driver string, opts, labels map[string]string) error {
	if _, err := c.api.VolumeInspect(ctx, name); err == nil {
		return nil
	} else if !cerrdefs.IsNotFound(err) {
		return fmt.Errorf("inspect volume %s: %w", name, err)
	}
	_, err := c.api.VolumeCreate(ctx, volume.CreateOptions{Name: name, Driver: driver, DriverOpts: opts, Labels: labels})
	if err != nil {
		return fmt.Errorf("create volume %s: %w", name, err)
	}
	return nil
}

// RemoveVolume implements Engine.
func (c *Client) RemoveVolume(ctx context.Context, name string) error {
	if err := c.api.VolumeRemove(ctx, name, true); err != nil && !cerrdefs.IsNotFound(err) {
		return fmt.Errorf("remove volume %s: %w", name, err)
	}
	return nil
}

// CreateContainer implements Engine.
func (c *Client) CreateContainer(ctx context.Context, spec ContainerSpec) (string, error) {
	cfg, host, net := BuildConfigs(spec)
	resp, err := c.api.ContainerCreate(ctx, cfg, host, net, nil, spec.Name)
	if err != nil {
		return "", fmt.Errorf("create container %s: %w", spec.Name, err)
	}
	return resp.ID, nil
}

// StartContainer implements Engine.
func (c *Client) StartContainer(ctx context.Context, id string) error {
	if err := c.api.ContainerStart(ctx, id, container.StartOptions{}); err != nil {
		return fmt.Errorf("start container: %w", notFound(err))
	}
	return nil
}

// StopContainer implements Engine.
func (c *Client) StopContainer(ctx context.Context, id string, timeout time.Duration) error {
	secs := int(timeout / time.Second)
	opts := container.StopOptions{Timeout: &secs}
	if timeout <= 0 {
		opts.Signal = "SIGKILL"
		zero := 0
		opts.Timeout = &zero
	}
	if err := c.api.ContainerStop(ctx, id, opts); err != nil {
		if cerrdefs.IsNotFound(err) {
			return ErrNotFound
		}
		return fmt.Errorf("stop container: %w", err)
	}
	return nil
}

// RemoveContainer implements Engine.
func (c *Client) RemoveContainer(ctx context.Context, id string) error {
	err := c.api.ContainerRemove(ctx, id, container.RemoveOptions{Force: true, RemoveVolumes: false})
	if err != nil && !cerrdefs.IsNotFound(err) {
		return fmt.Errorf("remove container: %w", err)
	}
	return nil
}

// InspectContainer implements Engine.
func (c *Client) InspectContainer(ctx context.Context, id string) (Container, error) {
	info, err := c.api.ContainerInspect(ctx, id)
	if err != nil {
		return Container{}, notFound(err)
	}
	out := Container{ID: info.ID, IPs: map[string]string{}}
	if info.ContainerJSONBase != nil {
		out.Name = trimSlash(info.Name)
		out.Running = info.State != nil && info.State.Running
	}
	if info.Config != nil {
		out.Image = info.Config.Image
		out.Labels = info.Config.Labels
	}
	if info.NetworkSettings != nil {
		for name, ep := range info.NetworkSettings.Networks {
			if ep != nil {
				out.IPs[name] = ep.IPAddress
			}
		}
	}
	return out, nil
}

// ListContainers implements Engine.
func (c *Client) ListContainers(ctx context.Context, labels map[string]string) ([]Container, error) {
	f := filters.NewArgs()
	for k, v := range labels {
		f.Add("label", k+"="+v)
	}
	list, err := c.api.ContainerList(ctx, container.ListOptions{All: true, Filters: f})
	if err != nil {
		return nil, fmt.Errorf("list containers: %w", err)
	}
	out := make([]Container, 0, len(list))
	for _, s := range list {
		ct := Container{ID: s.ID, Image: s.Image, Labels: s.Labels, Running: s.State == "running", IPs: map[string]string{}}
		if len(s.Names) > 0 {
			ct.Name = trimSlash(s.Names[0])
		}
		if s.NetworkSettings != nil {
			for name, ep := range s.NetworkSettings.Networks {
				if ep != nil {
					ct.IPs[name] = ep.IPAddress
				}
			}
		}
		out = append(out, ct)
	}
	return out, nil
}

// CopyTo implements Engine.
func (c *Client) CopyTo(ctx context.Context, id, dir string, tarStream io.Reader) error {
	// CopyUIDGID makes the daemon chown extracted files to the container's configured user
	// (the non-root agent uid) instead of root.
	opts := container.CopyToContainerOptions{CopyUIDGID: true}
	if err := c.api.CopyToContainer(ctx, id, dir, tarStream, opts); err != nil {
		return fmt.Errorf("copy to container: %w", notFound(err))
	}
	return nil
}

// CopyFrom implements Engine.
func (c *Client) CopyFrom(ctx context.Context, id, path string) (io.ReadCloser, error) {
	rc, _, err := c.api.CopyFromContainer(ctx, id, path)
	if err != nil {
		return nil, fmt.Errorf("copy from container: %w", notFound(err))
	}
	return rc, nil
}

// Signal implements Engine.
func (c *Client) Signal(ctx context.Context, name, signal string) error {
	if err := c.api.ContainerKill(ctx, name, signal); err != nil {
		return fmt.Errorf("signal %s to %s: %w", signal, name, notFound(err))
	}
	return nil
}

// MemoryUsage implements Engine. Same number `docker stats` shows: usage minus inactive file cache.
func (c *Client) MemoryUsage(ctx context.Context, id string) (int64, error) {
	resp, err := c.api.ContainerStatsOneShot(ctx, id)
	if err != nil {
		return 0, fmt.Errorf("stats: %w", notFound(err))
	}
	defer func() { _ = resp.Body.Close() }()
	var s container.StatsResponse
	if err := json.NewDecoder(resp.Body).Decode(&s); err != nil {
		return 0, fmt.Errorf("decode stats: %w", err)
	}
	return MemoryFromStats(s.MemoryStats), nil
}

// MemoryFromStats mirrors the docker CLI calculation (cgroup v2: inactive_file; v1: total_inactive_file).
func MemoryFromStats(m container.MemoryStats) int64 {
	usage := m.Usage
	if v, ok := m.Stats["inactive_file"]; ok && v < usage {
		usage -= v
	} else if v, ok := m.Stats["total_inactive_file"]; ok && v < usage {
		usage -= v
	}
	return int64(usage)
}

func trimSlash(s string) string {
	if len(s) > 0 && s[0] == '/' {
		return s[1:]
	}
	return s
}
