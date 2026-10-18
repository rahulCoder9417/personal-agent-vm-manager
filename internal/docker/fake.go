package docker

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"path"
	"sort"
	"strings"
	"sync"
	"time"
)

// Fake is an in-memory Engine for unit tests. Volumes hold files so CopyTo /
// CopyFrom round-trip like the real daemon (only paths under a mounted volume
// are writable, like a read-only rootfs).
type Fake struct {
	mu         sync.Mutex
	nextID     int
	nextIP     int
	containers map[string]*FakeContainer // by id
	volumes    map[string]*FakeVolume

	// StartErrors: how many upcoming StartContainer calls fail, per container name.
	StartErrors map[string]int
	// OnStart runs (outside the lock) after a container starts.
	OnStart func(c Container)
	// OnStop runs (outside the lock) after a container stops.
	OnStop func(c Container)
	// SizeOverride sets what VolumeSize reports for a volume (bytes), for tests.
	SizeOverride map[string]int64
	// Memory returned by MemoryUsage for running containers.
	Memory int64

	Signals []string // "container:SIGNAL"
	Stops   []string // "name:timeout"
}

// FakeContainer is a fake container's state.
type FakeContainer struct {
	ID      string
	Spec    ContainerSpec
	Running bool
	IP      string
	Starts  int
}

// FakeVolume is a fake volume's contents.
type FakeVolume struct {
	Driver string
	Opts   map[string]string
	Labels map[string]string
	Files  map[string][]byte // path relative to the volume root, e.g. ".platform/platform.json"
	Modes  map[string]int64
	UIDs   map[string]int
}

// NewFake returns an empty fake daemon.
func NewFake() *Fake {
	return &Fake{
		containers:  map[string]*FakeContainer{},
		volumes:     map[string]*FakeVolume{},
		StartErrors: map[string]int{},
		Memory:      200 * 1024 * 1024,
	}
}

func (f *Fake) find(idOrName string) *FakeContainer {
	if c, ok := f.containers[idOrName]; ok {
		return c
	}
	for _, c := range f.containers {
		if c.Spec.Name == idOrName {
			return c
		}
	}
	return nil
}

func (c *FakeContainer) view() Container {
	ips := map[string]string{}
	if c.Running {
		ips[c.Spec.Network] = c.IP
	}
	labels := map[string]string{}
	for k, v := range c.Spec.Labels {
		labels[k] = v
	}
	return Container{ID: c.ID, Name: c.Spec.Name, Image: c.Spec.Image, Labels: labels, Running: c.Running, IPs: ips}
}

// EnsureVolume implements Engine.
func (f *Fake) EnsureVolume(_ context.Context, name, driver string, opts, labels map[string]string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.volumes[name]; !ok {
		f.volumes[name] = &FakeVolume{Driver: driver, Opts: opts, Labels: labels,
			Files: map[string][]byte{}, Modes: map[string]int64{}, UIDs: map[string]int{}}
	}
	return nil
}

// RemoveVolume implements Engine.
func (f *Fake) RemoveVolume(_ context.Context, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, c := range f.containers {
		if c.Spec.VolumeName == name {
			return fmt.Errorf("volume %s is in use", name)
		}
	}
	delete(f.volumes, name)
	return nil
}

// CreateContainer implements Engine.
func (f *Fake) CreateContainer(_ context.Context, spec ContainerSpec) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.find(spec.Name) != nil {
		return "", fmt.Errorf("conflict: container name %s in use", spec.Name)
	}
	if _, ok := f.volumes[spec.VolumeName]; !ok {
		// Docker auto-creates named volumes on use.
		f.volumes[spec.VolumeName] = &FakeVolume{Files: map[string][]byte{}, Modes: map[string]int64{}, UIDs: map[string]int{}}
	}
	f.nextID++
	id := fmt.Sprintf("cid%04d", f.nextID)
	f.containers[id] = &FakeContainer{ID: id, Spec: spec}
	return id, nil
}

// StartContainer implements Engine.
func (f *Fake) StartContainer(_ context.Context, id string) error {
	f.mu.Lock()
	c := f.find(id)
	if c == nil {
		f.mu.Unlock()
		return ErrNotFound
	}
	if n := f.StartErrors[c.Spec.Name]; n > 0 {
		f.StartErrors[c.Spec.Name] = n - 1
		f.mu.Unlock()
		return errors.New("fake: start failed")
	}
	if !c.Running {
		c.Running = true
		c.Starts++
		f.nextIP++
		c.IP = fmt.Sprintf("172.30.%d.%d", 1+f.nextIP/250, 2+f.nextIP%250)
	}
	view, hook := c.view(), f.OnStart
	f.mu.Unlock()
	if hook != nil {
		hook(view)
	}
	return nil
}

// StopContainer implements Engine.
func (f *Fake) StopContainer(_ context.Context, id string, timeout time.Duration) error {
	f.mu.Lock()
	c := f.find(id)
	if c == nil {
		f.mu.Unlock()
		return ErrNotFound
	}
	f.Stops = append(f.Stops, fmt.Sprintf("%s:%s", c.Spec.Name, timeout))
	wasRunning := c.Running
	c.Running = false
	view, hook := c.view(), f.OnStop
	f.mu.Unlock()
	if wasRunning && hook != nil {
		hook(view)
	}
	return nil
}

// RemoveContainer implements Engine.
func (f *Fake) RemoveContainer(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if c := f.find(id); c != nil {
		delete(f.containers, c.ID)
	}
	return nil
}

// InspectContainer implements Engine.
func (f *Fake) InspectContainer(_ context.Context, id string) (Container, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	c := f.find(id)
	if c == nil {
		return Container{}, ErrNotFound
	}
	return c.view(), nil
}

// ListContainers implements Engine.
func (f *Fake) ListContainers(_ context.Context, labels map[string]string) ([]Container, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := []Container{}
	for _, c := range f.containers {
		match := true
		for k, v := range labels {
			if c.Spec.Labels[k] != v {
				match = false
			}
		}
		if match {
			out = append(out, c.view())
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// volumePath maps an absolute container path to (volume, relative path).
func (f *Fake) volumePath(c *FakeContainer, p string) (*FakeVolume, string, error) {
	p = path.Clean(p)
	target := path.Clean(c.Spec.VolumeTarget)
	if p != target && !strings.HasPrefix(p, target+"/") {
		return nil, "", errors.New("fake: container rootfs is marked read-only")
	}
	v := f.volumes[c.Spec.VolumeName]
	if v == nil {
		return nil, "", ErrNotFound
	}
	return v, strings.TrimPrefix(strings.TrimPrefix(p, target), "/"), nil
}

// CopyTo implements Engine.
func (f *Fake) CopyTo(_ context.Context, id, dir string, tarStream io.Reader) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	c := f.find(id)
	if c == nil {
		return ErrNotFound
	}
	v, rel, err := f.volumePath(c, dir)
	if err != nil {
		return err
	}
	tr := tar.NewReader(tarStream)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if h.Typeflag != tar.TypeReg {
			continue
		}
		body, err := io.ReadAll(tr)
		if err != nil {
			return err
		}
		p := path.Join(rel, path.Clean(h.Name))
		v.Files[p] = body
		v.Modes[p] = h.Mode
		v.UIDs[p] = h.Uid
	}
}

// CopyFrom implements Engine. A file comes back as a tar with one entry named
// after its base name; a directory as a tar of its tree under its base name.
func (f *Fake) CopyFrom(_ context.Context, id, p string) (io.ReadCloser, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	c := f.find(id)
	if c == nil {
		return nil, ErrNotFound
	}
	v, rel, err := f.volumePath(c, p)
	if err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	base := path.Base(path.Clean(p))
	if body, ok := v.Files[rel]; ok {
		_ = tw.WriteHeader(&tar.Header{Name: base, Mode: 0o600, Size: int64(len(body)), Typeflag: tar.TypeReg})
		_, _ = tw.Write(body)
	} else {
		names := []string{}
		for name := range v.Files {
			if rel == "" || strings.HasPrefix(name, rel+"/") {
				names = append(names, name)
			}
		}
		if len(names) == 0 && rel != "" {
			return nil, ErrNotFound
		}
		sort.Strings(names)
		_ = tw.WriteHeader(&tar.Header{Name: base + "/", Mode: 0o755, Typeflag: tar.TypeDir})
		for _, name := range names {
			body := v.Files[name]
			inner := strings.TrimPrefix(strings.TrimPrefix(name, rel), "/")
			_ = tw.WriteHeader(&tar.Header{Name: path.Join(base, inner), Mode: 0o600, Size: int64(len(body)), Typeflag: tar.TypeReg})
			_, _ = tw.Write(body)
		}
	}
	_ = tw.Close()
	return io.NopCloser(&buf), nil
}

// Signal implements Engine.
func (f *Fake) Signal(_ context.Context, name, signal string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Signals = append(f.Signals, name+":"+signal)
	return nil
}

// VolumeSize implements Engine: the total of the volume's files, or SizeOverride[name] when set.
func (f *Fake) VolumeSize(_ context.Context, name string) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if n, ok := f.SizeOverride[name]; ok {
		return n, nil
	}
	v := f.volumes[name]
	if v == nil {
		return 0, nil
	}
	var n int64
	for _, b := range v.Files {
		n += int64(len(b))
	}
	return n, nil
}

// MemoryUsage implements Engine.
func (f *Fake) MemoryUsage(_ context.Context, id string) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	c := f.find(id)
	if c == nil {
		return 0, ErrNotFound
	}
	if !c.Running {
		return 0, nil
	}
	return f.Memory, nil
}

// --- test helpers ---

// Get returns a copy of a container by id or name.
func (f *Fake) Get(idOrName string) (FakeContainer, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	c := f.find(idOrName)
	if c == nil {
		return FakeContainer{}, false
	}
	return *c, true
}

// Volume returns a volume by name.
func (f *Fake) Volume(name string) (*FakeVolume, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	v, ok := f.volumes[name]
	return v, ok
}

// File returns a file's content from a volume.
func (f *Fake) File(volumeName, rel string) ([]byte, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	v, ok := f.volumes[volumeName]
	if !ok {
		return nil, false
	}
	b, ok := v.Files[rel]
	return b, ok
}

// Crash marks a container as not running without any hooks (simulates a crash / daemon restart).
func (f *Fake) Crash(idOrName string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if c := f.find(idOrName); c != nil {
		c.Running = false
	}
}

// RunningCount returns how many containers are running.
func (f *Fake) RunningCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, c := range f.containers {
		if c.Running {
			n++
		}
	}
	return n
}

// SignalCount returns how many signals were sent.
func (f *Fake) SignalCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.Signals)
}

var _ Engine = (*Fake)(nil)
var _ Engine = (*Client)(nil)
