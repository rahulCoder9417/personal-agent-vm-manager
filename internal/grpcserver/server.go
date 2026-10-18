// Package grpcserver exposes the sandbox manager as the vm.v1.VmManager gRPC
// service (proto/vm/v1/vm.proto) with shared-secret auth.
package grpcserver

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"errors"
	"log/slog"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	vmv1 "github.com/personal-agent/vm-manager/gen/vm/v1"
	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/sandbox"
	"github.com/personal-agent/vm-manager/internal/store"
)

// SecretHeader is the metadata key carrying the shared internal secret.
const SecretHeader = "x-internal-secret"

// Manager is the part of *sandbox.Manager the server uses.
type Manager interface {
	Create(ctx context.Context, req sandbox.CreateRequest) (store.Sandbox, string, error)
	WakeTimed(ctx context.Context, userID string, priority int32) (store.Sandbox, bool, sandbox.WakeTimings, error)
	Deliver(ctx context.Context, req sandbox.DeliverRequest, send func(sandbox.Event) error) error
	Touch(ctx context.Context, userID string) error
	Stop(ctx context.Context, userID string, force bool, reason string) (store.Sandbox, error)
	Delete(ctx context.Context, userID string) error
	Get(ctx context.Context, userID string) (sandbox.Info, error)
	List(ctx context.Context, states ...store.State) ([]sandbox.Info, error)
	Usage(ctx context.Context, userID string, since, until time.Time) ([]store.Usage, error)
	SetEgressAllowlist(ctx context.Context, userID string, domains []string, servers []sandbox.MCPServer) error
	Snapshot(ctx context.Context, userID, destination string) (string, int64, error)
	SetAgentRuntime(ctx context.Context, userID string, runtime store.Runtime) (store.Sandbox, error)
	StartClaudeLogin(ctx context.Context, userID string) (bridge.ClaudeLogin, error)
	CompleteClaudeLogin(ctx context.Context, userID, loginID, code string) (bridge.ClaudeLoginResult, error)
	ClaudeLogout(ctx context.Context, userID string) error
	SetBrowser(ctx context.Context, userID string, enabled bool) (store.Sandbox, error)
}

// Server implements vmv1.VmManagerServer.
type Server struct {
	vmv1.UnimplementedVmManagerServer
	m   Manager
	log *slog.Logger
}

// New builds the service implementation.
func New(m Manager, log *slog.Logger) *Server { return &Server{m: m, log: log} }

// NewGRPCServer builds a grpc.Server with auth + logging interceptors and registers the service.
func NewGRPCServer(m Manager, secret string, log *slog.Logger) *grpc.Server {
	a := &auth{want: sha256.Sum256([]byte(secret)), log: log}
	s := grpc.NewServer(
		grpc.ChainUnaryInterceptor(a.unary),
		grpc.ChainStreamInterceptor(a.stream),
	)
	vmv1.RegisterVmManagerServer(s, New(m, log))
	return s
}

// --- auth ---

type auth struct {
	want [32]byte
	log  *slog.Logger
}

func (a *auth) check(ctx context.Context, method string) error {
	md, _ := metadata.FromIncomingContext(ctx)
	vals := md.Get(SecretHeader)
	if len(vals) != 1 {
		a.log.Warn("grpc call without internal secret", "method", method)
		return status.Error(codes.Unauthenticated, "missing x-internal-secret")
	}
	got := sha256.Sum256([]byte(vals[0])) // hashing first makes the compare length-independent
	if subtle.ConstantTimeCompare(got[:], a.want[:]) != 1 {
		a.log.Warn("grpc call with a wrong internal secret", "method", method)
		return status.Error(codes.Unauthenticated, "invalid x-internal-secret")
	}
	return nil
}

func (a *auth) unary(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
	if err := a.check(ctx, info.FullMethod); err != nil {
		return nil, err
	}
	start := time.Now()
	resp, err := handler(ctx, req)
	a.logResult(info.FullMethod, start, err, requestFields(req)...)
	return resp, err
}

type userIDGetter interface{ GetUserId() string }

// requestFields are the only request values ever logged: the user id, and a marker that the
// Claude sign-in code was present but redacted. Request bodies are never logged.
func requestFields(req any) []any {
	var f []any
	if r, ok := req.(userIDGetter); ok && r.GetUserId() != "" {
		f = append(f, "user_id", r.GetUserId())
	}
	if _, ok := req.(*vmv1.CompleteClaudeLoginRequest); ok {
		f = append(f, "code", "[REDACTED]")
	}
	return f
}

func (a *auth) stream(srv any, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
	if err := a.check(ss.Context(), info.FullMethod); err != nil {
		return err
	}
	start := time.Now()
	err := handler(srv, ss)
	a.logResult(info.FullMethod, start, err)
	return err
}

func (a *auth) logResult(method string, start time.Time, err error, fields ...any) {
	code := status.Code(err)
	lvl := slog.LevelDebug
	if code != codes.OK && code != codes.NotFound && code != codes.ResourceExhausted {
		lvl = slog.LevelWarn
	}
	args := append([]any{"method", method, "code", code.String(), "ms", time.Since(start).Milliseconds()}, fields...)
	a.log.Log(context.Background(), lvl, "grpc", args...)
}

// --- error mapping ---

func (s *Server) toStatus(err error) error {
	if err == nil {
		return nil
	}
	if _, ok := status.FromError(err); ok && status.Code(err) != codes.Unknown {
		return err
	}
	switch {
	case errors.Is(err, sandbox.ErrNotFound):
		return status.Error(codes.NotFound, "sandbox not found")
	case errors.Is(err, sandbox.ErrInvalid):
		return status.Error(codes.InvalidArgument, err.Error())
	case errors.Is(err, sandbox.ErrCapacity):
		return status.Error(codes.ResourceExhausted, "awake cap reached; retry later")
	case errors.Is(err, sandbox.ErrDiskFull):
		return status.Error(codes.ResourceExhausted, "disk_guard: server disk is nearly full; not waking sandboxes")
	case errors.Is(err, sandbox.ErrStorageLimit):
		return status.Error(codes.ResourceExhausted, "disk_limit: storage limit reached for this user")
	case errors.Is(err, sandbox.ErrBusy):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, sandbox.ErrUnavailable):
		return status.Error(codes.Unavailable, "sandbox could not be started; retry later")
	case errors.Is(err, sandbox.ErrLoginNotFound):
		return status.Error(codes.FailedPrecondition, "unknown or expired Claude sign-in; start again")
	case errors.Is(err, sandbox.ErrAgentUnavailable):
		return status.Error(codes.Unavailable, "the agent could not be reached; retry later")
	case errors.Is(err, context.Canceled):
		return status.Error(codes.Canceled, "canceled")
	case errors.Is(err, context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "deadline exceeded")
	}
	s.log.Error("internal error", "err", err)
	return status.Error(codes.Internal, "internal error")
}

// --- conversions ---

var stateToProto = map[store.State]vmv1.SandboxState{
	store.StateStopped: vmv1.SandboxState_SANDBOX_STATE_STOPPED,
	store.StateWaking:  vmv1.SandboxState_SANDBOX_STATE_WAKING,
	store.StateAwake:   vmv1.SandboxState_SANDBOX_STATE_AWAKE,
	store.StateBusy:    vmv1.SandboxState_SANDBOX_STATE_BUSY,
	store.StateBroken:  vmv1.SandboxState_SANDBOX_STATE_BROKEN,
}

func stateFromProto(s vmv1.SandboxState) (store.State, bool) {
	for k, v := range stateToProto {
		if v == s {
			return k, true
		}
	}
	return "", false
}

var runtimeToProto = map[store.Runtime]vmv1.AgentRuntime{
	store.RuntimeHermes:     vmv1.AgentRuntime_AGENT_RUNTIME_HERMES,
	store.RuntimeClaudeCode: vmv1.AgentRuntime_AGENT_RUNTIME_CLAUDE_CODE,
}

// runtimeFromProto: UNSPECIFIED means hermes; unknown values are rejected.
func runtimeFromProto(r vmv1.AgentRuntime) (store.Runtime, error) {
	switch r {
	case vmv1.AgentRuntime_AGENT_RUNTIME_UNSPECIFIED, vmv1.AgentRuntime_AGENT_RUNTIME_HERMES:
		return store.RuntimeHermes, nil
	case vmv1.AgentRuntime_AGENT_RUNTIME_CLAUDE_CODE:
		return store.RuntimeClaudeCode, nil
	}
	return "", status.Error(codes.InvalidArgument, "unknown runtime")
}

func toProto(sb store.Sandbox, memory int64) *vmv1.Sandbox {
	allow := sb.Allowlist
	if allow == nil {
		allow = []string{}
	}
	rt, ok := runtimeToProto[sb.Runtime]
	if !ok {
		rt = vmv1.AgentRuntime_AGENT_RUNTIME_HERMES
	}
	return &vmv1.Sandbox{
		UserId:             sb.UserID,
		ContainerId:        sb.ContainerID,
		ImageVersion:       sb.ImageVersion,
		State:              stateToProto[sb.State],
		LastActiveAtUnixMs: sb.LastActiveAt.UnixMilli(),
		MemoryBytes:        memory,
		Allowlist:          allow,
		Runtime:            rt,
		ClaudeConnected:    sb.ClaudeConnected,
		BrowserEnabled:     sb.BrowserEnabled,
	}
}

func mcpServers(in []*vmv1.UserMcpServer) []sandbox.MCPServer {
	out := make([]sandbox.MCPServer, 0, len(in))
	for _, s := range in {
		out = append(out, sandbox.MCPServer{ID: s.GetId(), Name: s.GetName(), URL: s.GetUrl(),
			AuthHeaderName: s.GetAuthHeaderName(), AuthSecret: s.GetAuthSecret()})
	}
	return out
}

var kindToBridge = map[vmv1.DeliverKind]string{
	vmv1.DeliverKind_DELIVER_KIND_MESSAGE:         "message",
	vmv1.DeliverKind_DELIVER_KIND_APPROVAL_ANSWER: "approval_answer",
	vmv1.DeliverKind_DELIVER_KIND_AUTOMATION:      "automation",
	vmv1.DeliverKind_DELIVER_KIND_APP_EVENT:       "app_event",
}

var eventToProto = map[string]vmv1.DeliverEventType{
	bridge.EventAccepted:  vmv1.DeliverEventType_DELIVER_EVENT_TYPE_ACCEPTED,
	bridge.EventWorking:   vmv1.DeliverEventType_DELIVER_EVENT_TYPE_WORKING,
	bridge.EventTextDelta: vmv1.DeliverEventType_DELIVER_EVENT_TYPE_TEXT_DELTA,
	bridge.EventTurnDone:  vmv1.DeliverEventType_DELIVER_EVENT_TYPE_TURN_DONE,
	bridge.EventError:     vmv1.DeliverEventType_DELIVER_EVENT_TYPE_ERROR,
}

// --- RPCs ---

// CreateSandbox implements vmv1.VmManagerServer.
func (s *Server) CreateSandbox(ctx context.Context, req *vmv1.CreateSandboxRequest) (*vmv1.CreateSandboxResponse, error) {
	rt, err := runtimeFromProto(req.GetRuntime())
	if err != nil {
		return nil, err
	}
	sb, hash, err := s.m.Create(ctx, sandbox.CreateRequest{
		UserID: req.GetUserId(), LiteLLMKey: req.GetLitellmKey(), Priority: int32(req.GetPriority()),
		Allowlist: req.GetAllowlist(), MCPServers: mcpServers(req.GetMcpServers()), Timezone: req.GetTimezone(),
		Runtime: rt, BrowserEnabled: req.GetBrowserEnabled(),
	})
	if err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.CreateSandboxResponse{Sandbox: toProto(sb, 0), McpTokenHash: hash}, nil
}

// WakeSandbox implements vmv1.VmManagerServer.
func (s *Server) WakeSandbox(ctx context.Context, req *vmv1.WakeSandboxRequest) (*vmv1.WakeSandboxResponse, error) {
	sb, was, t, err := s.m.WakeTimed(ctx, req.GetUserId(), int32(req.GetPriority()))
	if err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.WakeSandboxResponse{Sandbox: toProto(sb, 0), WasStopped: was, Timings: &vmv1.WakeTimings{
		LockWaitMs: t.LockWait.Milliseconds(), CapacityWaitMs: t.CapacityWait.Milliseconds(), Evicted: t.Evicted,
		Recreated: t.Recreated, ContainerStartMs: t.ContainerStart.Milliseconds(),
		BridgeReadyMs: t.BridgeReady.Milliseconds(),
	}}, nil
}

// Deliver implements vmv1.VmManagerServer.
func (s *Server) Deliver(req *vmv1.DeliverRequest, stream grpc.ServerStreamingServer[vmv1.DeliverEvent]) error {
	kind, ok := kindToBridge[req.GetKind()]
	if !ok {
		return status.Error(codes.InvalidArgument, "kind must be set")
	}
	if req.GetTimeoutSeconds() < 0 {
		return status.Error(codes.InvalidArgument, "timeout_seconds must be >= 0")
	}
	atts := make([]bridge.Attachment, 0, len(req.GetAttachments()))
	for _, a := range req.GetAttachments() {
		atts = append(atts, bridge.Attachment{ID: a.GetId(), Name: a.GetName(), MimeType: a.GetMimeType(), URL: a.GetUrl()})
	}
	dr := sandbox.DeliverRequest{
		UserID: req.GetUserId(),
		Bridge: bridge.DeliverRequest{
			DeliveryID: req.GetDeliveryId(), Kind: kind, SessionKey: req.GetSessionKey(), Channel: req.GetChannel(),
			Text: req.GetText(), Attachments: atts, ApprovalID: req.GetApprovalId(), Approved: req.GetApproved(),
		},
		MetadataJSON: req.GetMetadataJson(),
		Timeout:      time.Duration(req.GetTimeoutSeconds()) * time.Second,
	}
	err := s.m.Deliver(stream.Context(), dr, func(ev sandbox.Event) error {
		return stream.Send(&vmv1.DeliverEvent{
			Type: eventToProto[ev.Type], Message: ev.Message, PlatformFault: ev.PlatformFault, AtUnixMs: ev.At.UnixMilli(),
			TimingsJson: ev.Timings,
		})
	})
	return s.toStatus(err)
}

// Touch implements vmv1.VmManagerServer.
func (s *Server) Touch(ctx context.Context, req *vmv1.TouchRequest) (*vmv1.TouchResponse, error) {
	if err := s.m.Touch(ctx, req.GetUserId()); err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.TouchResponse{}, nil
}

// StopSandbox implements vmv1.VmManagerServer.
func (s *Server) StopSandbox(ctx context.Context, req *vmv1.StopSandboxRequest) (*vmv1.StopSandboxResponse, error) {
	sb, err := s.m.Stop(ctx, req.GetUserId(), req.GetForce(), req.GetReason())
	if err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.StopSandboxResponse{Sandbox: toProto(sb, 0)}, nil
}

// DeleteSandbox implements vmv1.VmManagerServer.
func (s *Server) DeleteSandbox(ctx context.Context, req *vmv1.DeleteSandboxRequest) (*vmv1.DeleteSandboxResponse, error) {
	if err := s.m.Delete(ctx, req.GetUserId()); err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.DeleteSandboxResponse{}, nil
}

// GetSandbox implements vmv1.VmManagerServer.
func (s *Server) GetSandbox(ctx context.Context, req *vmv1.GetSandboxRequest) (*vmv1.Sandbox, error) {
	info, err := s.m.Get(ctx, req.GetUserId())
	if err != nil {
		return nil, s.toStatus(err)
	}
	return toProto(info.Sandbox, info.MemoryBytes), nil
}

// ListSandboxes implements vmv1.VmManagerServer.
func (s *Server) ListSandboxes(ctx context.Context, req *vmv1.ListSandboxesRequest) (*vmv1.ListSandboxesResponse, error) {
	states := make([]store.State, 0, len(req.GetStates()))
	for _, ps := range req.GetStates() {
		st, ok := stateFromProto(ps)
		if !ok {
			return nil, status.Error(codes.InvalidArgument, "unknown state")
		}
		states = append(states, st)
	}
	list, err := s.m.List(ctx, states...)
	if err != nil {
		return nil, s.toStatus(err)
	}
	out := &vmv1.ListSandboxesResponse{Sandboxes: make([]*vmv1.Sandbox, 0, len(list))}
	for _, info := range list {
		out.Sandboxes = append(out.Sandboxes, toProto(info.Sandbox, info.MemoryBytes))
	}
	return out, nil
}

// GetUsage implements vmv1.VmManagerServer.
func (s *Server) GetUsage(ctx context.Context, req *vmv1.GetUsageRequest) (*vmv1.GetUsageResponse, error) {
	if req.GetSinceUnixMs() < 0 || req.GetUntilUnixMs() < 0 {
		return nil, status.Error(codes.InvalidArgument, "timestamps must be >= 0")
	}
	since := time.UnixMilli(req.GetSinceUnixMs()).UTC()
	var until time.Time
	if req.GetUntilUnixMs() > 0 {
		until = time.UnixMilli(req.GetUntilUnixMs()).UTC()
	}
	usage, err := s.m.Usage(ctx, req.GetUserId(), since, until)
	if err != nil {
		return nil, s.toStatus(err)
	}
	out := &vmv1.GetUsageResponse{Usage: make([]*vmv1.UserUsage, 0, len(usage))}
	for _, u := range usage {
		out.Usage = append(out.Usage, &vmv1.UserUsage{UserId: u.UserID, AwakeSeconds: u.AwakeSeconds, BrowserSeconds: u.BrowserSeconds})
	}
	return out, nil
}

// SetEgressAllowlist implements vmv1.VmManagerServer.
func (s *Server) SetEgressAllowlist(ctx context.Context, req *vmv1.SetEgressAllowlistRequest) (*vmv1.SetEgressAllowlistResponse, error) {
	if err := s.m.SetEgressAllowlist(ctx, req.GetUserId(), req.GetDomains(), mcpServers(req.GetMcpServers())); err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.SetEgressAllowlistResponse{}, nil
}

// SnapshotVolume implements vmv1.VmManagerServer.
func (s *Server) SnapshotVolume(ctx context.Context, req *vmv1.SnapshotVolumeRequest) (*vmv1.SnapshotVolumeResponse, error) {
	loc, size, err := s.m.Snapshot(ctx, req.GetUserId(), req.GetDestination())
	if err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.SnapshotVolumeResponse{Location: loc, SizeBytes: size}, nil
}

// SetAgentRuntime implements vmv1.VmManagerServer.
func (s *Server) SetAgentRuntime(ctx context.Context, req *vmv1.SetAgentRuntimeRequest) (*vmv1.SetAgentRuntimeResponse, error) {
	rt, err := runtimeFromProto(req.GetRuntime())
	if err != nil {
		return nil, err
	}
	sb, err := s.m.SetAgentRuntime(ctx, req.GetUserId(), rt)
	if err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.SetAgentRuntimeResponse{Sandbox: toProto(sb, 0)}, nil
}

// StartClaudeLogin implements vmv1.VmManagerServer.
func (s *Server) StartClaudeLogin(ctx context.Context, req *vmv1.StartClaudeLoginRequest) (*vmv1.StartClaudeLoginResponse, error) {
	l, err := s.m.StartClaudeLogin(ctx, req.GetUserId())
	if err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.StartClaudeLoginResponse{LoginId: l.LoginID, AuthorizeUrl: l.AuthorizeURL, ExpiresAtUnixMs: l.ExpiresAt}, nil
}

// CompleteClaudeLogin implements vmv1.VmManagerServer. The code is passed straight through
// and never logged (the interceptor logs it as [REDACTED]).
func (s *Server) CompleteClaudeLogin(ctx context.Context, req *vmv1.CompleteClaudeLoginRequest) (*vmv1.CompleteClaudeLoginResponse, error) {
	res, err := s.m.CompleteClaudeLogin(ctx, req.GetUserId(), req.GetLoginId(), req.GetCode())
	if err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.CompleteClaudeLoginResponse{Connected: res.Connected, Message: res.Message}, nil
}

// ClaudeLogout implements vmv1.VmManagerServer.
func (s *Server) ClaudeLogout(ctx context.Context, req *vmv1.ClaudeLogoutRequest) (*vmv1.ClaudeLogoutResponse, error) {
	if err := s.m.ClaudeLogout(ctx, req.GetUserId()); err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.ClaudeLogoutResponse{}, nil
}

// SetBrowser implements vmv1.VmManagerServer. The switch is stored; the container gets it on
// its next wake.
func (s *Server) SetBrowser(ctx context.Context, req *vmv1.SetBrowserRequest) (*vmv1.SetBrowserResponse, error) {
	sb, err := s.m.SetBrowser(ctx, req.GetUserId(), req.GetEnabled())
	if err != nil {
		return nil, s.toStatus(err)
	}
	return &vmv1.SetBrowserResponse{Sandbox: toProto(sb, 0)}, nil
}
