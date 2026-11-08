//! The jobs' cron, parsed as Python's `awm.config.cron` did, and routine's schedule
//! arguments built from a job row. These replace the in-process scheduler's tests.

use std::collections::BTreeSet;

use awm_plugin_dvc::clock::{self, JITTER_S, PROJECT};
use awm_plugin_dvc::cron::{parse, to_on_calendar};
use awm_plugin_dvc::jobs::{spec_for, CACHE_SYNC, JOBS, WORKSPACE_BACKUP};
use awm_plugin_dvc::runs::Row;
use chrono::{Local, TimeZone};
use serde_json::{json, Value};

fn at(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> f64 {
    Local.with_ymd_and_hms(y, mo, d, h, mi, 0).unwrap().timestamp() as f64
}

fn set(v: &[u32]) -> BTreeSet<u32> {
    v.iter().copied().collect()
}

// -- parsing --------------------------------------------------------------------------------

#[test]
fn steps_ranges_and_lists_expand_to_value_sets() {
    let c = parse("*/15 9-11 1,15,31 10-12/2 1-5").unwrap();
    assert_eq!(c.sets[0], set(&[0, 15, 30, 45]));
    assert_eq!(c.sets[1], set(&[9, 10, 11]));
    assert_eq!(c.sets[2], set(&[1, 15, 31]));
    assert_eq!(c.sets[3], set(&[10, 12]));
    assert_eq!(c.sets[4], set(&[1, 2, 3, 4, 5]));
}

#[test]
fn a_star_is_the_full_range_of_its_field() {
    let c = parse("* * * * *").unwrap();
    assert_eq!(c.sets[0].len(), 60);
    assert_eq!(c.sets[1].len(), 24);
    assert_eq!(c.sets[2], (1..=31).collect());
    assert_eq!(c.sets[3], (1..=12).collect());
    assert_eq!(c.sets[4], (0..=6).collect());
}

#[test]
fn a_mixed_list_of_ranges_steps_and_values_unions() {
    let c = parse("0-10/5,30,50-52 * * * *").unwrap();
    assert_eq!(c.sets[0], set(&[0, 5, 10, 30, 50, 51, 52]));
}

#[test]
fn a_malformed_spec_raises_rather_than_being_quietly_ignored() {
    for spec in ["", "   ", "0 4 * *", "0 4 * * * *", "nonsense", "60 4 * * *", "@every 0m", "@every 5d", "@every 5m"] {
        assert!(parse(spec).is_err(), "{spec:?} parsed");
    }
}

#[test]
fn values_outside_a_fields_bounds_are_refused() {
    for spec in ["0 24 * * *", "0 0 0 * *", "0 0 32 * *", "0 0 * 0 *", "0 0 * 13 *", "0 0 * * 7", "0-60 * * * *"] {
        let e = parse(spec).unwrap_err();
        assert!(e.0.contains("out of"), "{spec:?}: {}", e.0);
    }
}

#[test]
fn bad_steps_ranges_and_tokens_are_refused() {
    for (spec, why) in [
        ("*/0 * * * *", "bad step"),
        ("*/x * * * *", "bad step"),
        ("*/ * * * *", "bad step"),
        ("1-x * * * *", "bad range"),
        ("-1 * * * *", "bad range"),
        ("5-1 * * * *", "out of"),
        ("mon * * * *", "unrecognised"),
        ("1,,2 * * * *", "empty"),
    ] {
        let e = parse(spec).unwrap_err();
        assert!(e.0.contains(why), "{spec:?}: {}", e.0);
    }
}

#[test]
fn the_two_default_schedules_parse() {
    for j in &JOBS {
        parse(j.cron).unwrap();
        to_on_calendar(j.cron).unwrap();
    }
}

// -- next_due -------------------------------------------------------------------------------

#[test]
fn a_daily_cron_lands_on_the_next_occurrence() {
    assert_eq!(parse("0 4 * * *").unwrap().next_due(at(2026, 8, 13, 5, 0)).unwrap(), at(2026, 8, 14, 4, 0));
}

#[test]
fn a_daily_cron_later_the_same_day_stays_that_day() {
    assert_eq!(parse("30 5 * * *").unwrap().next_due(at(2026, 8, 13, 4, 15)).unwrap(), at(2026, 8, 13, 5, 30));
}

#[test]
fn next_due_is_strictly_after_so_a_fire_cannot_refire_itself() {
    let now = at(2026, 8, 13, 4, 0);
    assert!(parse("0 4 * * *").unwrap().next_due(now).unwrap() > now);
    assert!(parse("* * * * *").unwrap().next_due(now + 30.0).unwrap() == now + 60.0);
}

#[test]
fn day_of_month_and_day_of_week_are_ored_when_both_are_restricted() {
    // Saturday 2026-11-07. The 10th is a Tuesday; the next Monday is the 9th.
    let sat = at(2026, 11, 7, 0, 0);
    let c = parse("0 0 10 * 1").unwrap();
    let first = c.next_due(sat).unwrap();
    assert_eq!(first, at(2026, 11, 9, 0, 0));
    let second = c.next_due(first).unwrap();
    assert_eq!(second, at(2026, 11, 10, 0, 0));
    assert_eq!(c.next_due(second).unwrap(), at(2026, 11, 16, 0, 0));
}

#[test]
fn one_restricted_day_field_alone_decides() {
    let sat = at(2026, 11, 7, 0, 0);
    assert_eq!(parse("0 0 10 * *").unwrap().next_due(sat).unwrap(), at(2026, 11, 10, 0, 0));
    assert_eq!(parse("0 0 * * 1").unwrap().next_due(sat).unwrap(), at(2026, 11, 9, 0, 0));
}

#[test]
fn a_starred_step_still_counts_as_restricted_as_python_reads_it() {
    // `*/1` is not the literal `*`, so the dom field is restricted, every day matches it,
    // and the OR fires daily even though dow names only Sunday.
    let sat = at(2026, 11, 7, 0, 0);
    assert_eq!(parse("0 3 */1 * 0").unwrap().next_due(sat).unwrap(), at(2026, 11, 7, 3, 0));
}

#[test]
fn an_unsatisfiable_cron_errors_rather_than_looping() {
    let e = parse("0 0 31 2 *").unwrap_err();
    assert!(e.0.contains("within a year"), "{}", e.0);
}

// -- the clock: routine's schedule args from a job row --------------------------------------

fn row(v: Value) -> Row {
    v.as_object().unwrap().clone()
}

#[test]
fn an_enabled_row_is_scheduled_at_its_cron_with_its_window_and_last_fire() {
    let spec = spec_for(CACHE_SYNC).unwrap();
    let last: f64 = 1_791_074_502.508_852_2;
    let r = row(json!({"name": CACHE_SYNC, "cron": "15 2 * * *", "enabled": 1, "catchup_window_s": 72000, "last_fire_at": last}));

    let args = clock::schedule_args(spec, &r).unwrap().unwrap();

    assert_eq!(
        args,
        json!({
            "project": PROJECT,
            "name": "dvc-cache-sync",
            "timer": "*-*-* 02:15:00",
            "jitter_s": JITTER_S,
            "catchup_s": 72000,
            "last_slot": "2026-10-04T00:41:42+00:00",
        })
    );
    assert_eq!(args["jitter_s"], 60);
    assert_eq!(args["last_slot"], awm_plugin_dvc::iso(last.floor()));
}

#[test]
fn a_row_never_fired_has_no_last_slot_and_falls_back_to_the_default_cron() {
    let spec = spec_for(WORKSPACE_BACKUP).unwrap();
    let r = row(json!({"name": WORKSPACE_BACKUP, "enabled": 1, "catchup_window_s": 3600, "last_fire_at": 0.0}));

    let args = clock::schedule_args(spec, &r).unwrap().unwrap();

    assert_eq!(args["name"], "dvc-workspace-backup");
    assert_eq!(args["timer"], "*-*-* 05:30:00");
    assert_eq!(args["catchup_s"], 3600);
    assert!(args.get("last_slot").is_none());
}

#[test]
fn a_disabled_row_is_unscheduled() {
    let spec = spec_for(CACHE_SYNC).unwrap();
    let r = row(json!({"name": CACHE_SYNC, "cron": "0 4 * * *", "enabled": 0, "catchup_window_s": 72000}));
    assert_eq!(clock::schedule_args(spec, &r).unwrap(), None);
}

#[test]
fn a_row_whose_cron_cannot_become_a_timer_is_an_error() {
    let spec = spec_for(CACHE_SYNC).unwrap();
    for cron in ["@every 5m", "0 4 1 * 1", "not a cron"] {
        let r = row(json!({"name": CACHE_SYNC, "cron": cron, "enabled": 1}));
        assert!(clock::schedule_args(spec, &r).is_err(), "{cron}");
    }
}
