package main

import (
	"testing"
	"time"

	"afxdp-cp/proto"
)

func reg1(role string) proto.Registration {
	return proto.Registration{Node: proto.NodeInfo{InstanceID: "i-" + role, PrivateIP: "10.0.0." + role[:1], Role: role}}
}

func TestRegistryUpsertAndList(t *testing.T) {
	r := NewRegistry(30)
	r.Upsert(reg1("source"))
	r.Upsert(reg1("source")) // idempotent
	if got := len(r.List()); got != 1 {
		t.Fatalf("want 1 node, got %d", got)
	}
	n := r.List()[0]
	if !n.Online || n.State != "idle" {
		t.Fatalf("fresh node should be online/idle: %+v", n)
	}
}

func TestRegistryHeartbeatAndStale(t *testing.T) {
	r := NewRegistry(10)
	r.Upsert(reg1("replicator"))
	// fresh heartbeat keeps it online + updates state
	if n, changed := r.Heartbeat(proto.Heartbeat{InstanceID: "i-replicator", Unix: time.Now().Unix(), State: "running"}); n == nil || !changed {
		t.Fatal("state change should report changed")
	}
	if r.List()[0].State != "running" {
		t.Fatal("heartbeat state not applied")
	}
	// a second identical heartbeat is NOT a material change (no re-broadcast)
	if _, changed := r.Heartbeat(proto.Heartbeat{InstanceID: "i-replicator", Unix: time.Now().Unix(), State: "running"}); changed {
		t.Fatal("identical heartbeat should not report changed")
	}
	// stale heartbeat (100s ago, window 10s) -> offline in List()
	r.Heartbeat(proto.Heartbeat{InstanceID: "i-replicator", Unix: time.Now().Unix() - 100, State: "running"})
	if r.List()[0].Online {
		t.Fatal("node past stale window should be offline")
	}
	// unknown heartbeat returns nil
	if n, _ := r.Heartbeat(proto.Heartbeat{InstanceID: "i-nope", Unix: time.Now().Unix()}); n != nil {
		t.Fatal("heartbeat for unknown node should return nil")
	}
}

func TestRegistryRoleScoping(t *testing.T) {
	r := NewRegistry(30)
	r.Upsert(reg1("source"))
	r.Upsert(reg1("replicator"))
	r.Upsert(reg1("destination"))
	if len(r.Online()) != 3 {
		t.Fatalf("Online want 3, got %d", len(r.Online()))
	}
	if s := r.ByRole("source"); s == nil || s.InstanceID != "i-source" {
		t.Fatalf("ByRole(source) wrong: %+v", s)
	}
	if d := r.AllByRole("destination"); len(d) != 1 {
		t.Fatalf("AllByRole(destination) want 1, got %d", len(d))
	}
	if r.ByRole("missing") != nil {
		t.Fatal("ByRole(missing) should be nil")
	}
}
