package ids

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"
)

func TestValidUserID(t *testing.T) {
	good := []string{"u1", "user_123", "0b9f-4c", strings.Repeat("a", 128)}
	bad := []string{"", "_x", "-x", "a/b", "a b", "a.b", "../etc", strings.Repeat("a", 129), "ü"}
	for _, id := range good {
		if !ValidUserID(id) {
			t.Errorf("%q should be valid", id)
		}
	}
	for _, id := range bad {
		if ValidUserID(id) {
			t.Errorf("%q should be invalid", id)
		}
	}
}

func TestSecretAndHash(t *testing.T) {
	a, err := NewSecret()
	if err != nil {
		t.Fatal(err)
	}
	b, _ := NewSecret()
	if a == b || len(a) != 43 {
		t.Fatalf("secrets must be random 32-byte base64url: %q %q", a, b)
	}
	sum := sha256.Sum256([]byte(a))
	if Hash(a) != hex.EncodeToString(sum[:]) {
		t.Fatal("hash must be lowercase hex sha256 of the token string")
	}
	if Hash(a) != strings.ToLower(Hash(a)) || len(Hash(a)) != 64 {
		t.Fatal("hash must be 64 lowercase hex chars")
	}
}

func TestNames(t *testing.T) {
	if ContainerName("u1") != "pa-sbx-u1" || VolumeName("u1") != "pa-data-u1" {
		t.Fatal("unexpected names")
	}
	if Short("u1") == Short("u2") || len(Short("u1")) != 16 {
		t.Fatal("short hash must be 16 hex chars and differ per user")
	}
}
