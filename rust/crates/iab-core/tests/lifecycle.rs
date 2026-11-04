//! Behaviour of the core on a SQLite file, one behaviour per test.
#![expect(clippy::unwrap_used, reason = "test helpers: a failure is the test failing")]

use iab_core::Bus;
use serde_json::Value;

fn bus() -> (tempfile::TempDir, Bus) {
    let dir = tempfile::tempdir().unwrap();
    let bus = Bus::at(dir.path().join("bus.db"));
    bus.register_agent("alpha@conf", "", "", None).unwrap();
    (dir, bus)
}

fn json(text: &str) -> Value {
    serde_json::from_str(text).unwrap()
}

#[test]
fn push_should_refuse_an_unknown_agent() {
    let (_dir, bus) = bus();
    let out = bus.push_task("ghost@conf", "t", "x", 1, 3, true).unwrap();
    assert!(
        out.starts_with("ERROR: agent 'ghost@conf' not registered."),
        "{out}"
    );
}

#[test]
fn push_should_refuse_a_duplicate_task_id() {
    let (_dir, bus) = bus();
    bus.push_task("alpha@conf", "t", "x", 1, 3, true).unwrap();
    let out = bus.push_task("alpha@conf", "t", "y", 1, 3, true).unwrap();
    assert_eq!(out, "ERROR: task_id 't' already exists.");
}

#[test]
fn claim_should_serve_the_highest_priority_first() {
    let (_dir, bus) = bus();
    bus.push_task("alpha@conf", "low", "x", 1, 3, true).unwrap();
    bus.push_task("alpha@conf", "high", "x", 9, 3, true).unwrap();
    let claimed = json(&bus.claim_task("alpha@conf", 60, None).unwrap());
    assert_eq!(claimed["task_id"], "high");
}

#[test]
fn claim_should_answer_no_task_on_an_empty_queue() {
    let (_dir, bus) = bus();
    assert_eq!(bus.claim_task("alpha@conf", 60, None).unwrap(), "NO_TASK");
}

#[test]
fn claim_should_reoffer_an_expired_lease_with_the_next_attempt() {
    let (_dir, bus) = bus();
    bus.push_task("alpha@conf", "t", "x", 1, 3, true).unwrap();
    bus.claim_task("alpha@conf", -1, None).unwrap();
    let again = json(&bus.claim_task("alpha@conf", 60, None).unwrap());
    assert_eq!(again["attempts"], 2);
}

#[test]
fn publish_should_refuse_a_stale_attempt() {
    let (_dir, bus) = bus();
    bus.push_task("alpha@conf", "t", "x", 1, 3, true).unwrap();
    bus.claim_task("alpha@conf", -1, None).unwrap();
    bus.claim_task("alpha@conf", 60, None).unwrap();
    let out = bus
        .publish_result("alpha@conf", "t", "late", Some(1), false)
        .unwrap();
    assert!(out.starts_with("ERROR: stale attempt token 1 < 2"), "{out}");
}

#[test]
fn claim_should_dead_letter_an_exhausted_task() {
    let (_dir, bus) = bus();
    bus.push_task("alpha@conf", "t", "x", 1, 1, true).unwrap();
    bus.claim_task("alpha@conf", -1, None).unwrap();
    assert_eq!(bus.claim_task("alpha@conf", 60, None).unwrap(), "NO_TASK");
}

#[test]
fn payload_should_come_back_byte_for_byte() {
    let (_dir, bus) = bus();
    let payload = "é — ✓ 🚀\r\nB\rC\n";
    bus.push_task("alpha@conf", "t", payload, 1, 3, true).unwrap();
    let claimed = json(&bus.claim_task("alpha@conf", 60, None).unwrap());
    assert_eq!(claimed["payload"], payload);
}

#[test]
fn poll_should_advance_the_cursor() {
    let (_dir, bus) = bus();
    bus.register_agent("beta@conf", "", "", None).unwrap();
    bus.notify("alpha@conf", "beta@conf", "bonjour", None, false)
        .unwrap();
    bus.poll("beta@conf", 100).unwrap();
    assert_eq!(bus.poll("beta@conf", 100).unwrap(), "[]");
}

#[test]
fn a_bare_harness_should_be_refused_when_ambiguous() {
    let (_dir, bus) = bus();
    bus.register_agent("alpha@other", "", "", None).unwrap();
    let out = bus.push_task("alpha", "t", "x", 1, 3, true).unwrap();
    assert!(out.starts_with("ERROR: 'alpha' is ambiguous"), "{out}");
}

#[test]
fn wait_signal_should_time_out_unsignalled() {
    let (_dir, bus) = bus();
    let snapshot = json(&bus.wait_signal("alpha@conf", "notification", 0.0, 0.2).unwrap());
    assert_eq!(snapshot["signalled"], false);
}
