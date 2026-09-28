// Package ids validates user ids, derives Docker names from them and makes
// the per-container secrets.
package ids

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"regexp"
)

// userIDRe keeps user ids safe to use in container, volume and file names.
// It must start with a letter or digit so it can't collide with our own files
// (for example "_vm-manager.conf" in the allowlist dir).
var userIDRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

// ValidUserID reports whether id is an acceptable user id.
func ValidUserID(id string) bool { return userIDRe.MatchString(id) }

// Short is a short stable hash of the user id (16 hex chars).
func Short(userID string) string {
	sum := sha256.Sum256([]byte(userID))
	return hex.EncodeToString(sum[:8])
}

// ContainerName is the Docker container name for a user's sandbox.
func ContainerName(userID string) string { return "pa-sbx-" + userID }

// VolumeName is the Docker volume (mounted at /data) for a user's sandbox.
func VolumeName(userID string) string { return "pa-data-" + userID }

// NewSecret returns 32 random bytes, base64url-encoded without padding.
func NewSecret() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("random secret: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// Hash is the lowercase hex sha256 of a token string. The backend hashes the
// bearer token it receives on /mcp the same way to find the user.
func Hash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}
