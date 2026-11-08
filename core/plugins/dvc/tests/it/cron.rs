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
    let star = parse("* * * * *").unwrap();
    assert_eq!((star.sets[0].len(), star.sets[1].len()), (60, 24));
    assert_eq!(star.sets[2], (1..=31).collect());
    assert_eq!(star.sets[4], (0..=6).collect());
    assert_eq!(parse("0-10/5,30,50-52 * * * *").unwrap().sets[0], set(&[0, 5, 10, 30, 50, 51, 52]));
    for j in &JOBS {
        parse(j.cron).unwrap();
        to_on_calendar(j.cron).unwrap();
    }
}

#[test]
fn a_malformed_spec_raises_rather_than_being_quietly_ignored() {
    for spec in ["", "   ", "0 4 * *", "0 4 * * * *", "nonsense", "60 4 * * *", "@every 0m", "@every 5d", "@every 5m"] {
        assert!(parse(spec).is_err(), "{spec:?} parsed");
    }
    for spec in ["0 24 * * *", "0 0 0 * *", "0 0 32 * *", "0 0 * 13 *", "0 0 * * 7", "0-60 * * * *"] {
        assert!(parse(spec).unwrap_err().0.contains("out of"), "{spec:?}");
    }
    for (spec, why) in [("*/0 * * * *", "bad step"), ("1-x * * * *", "bad range"), ("5-1 * * * *", "out of"), ("mon * * * *", "unrecognised"), ("1,,2 * * * *", "empty")] {
        assert!(parse(spec).unwrap_err().0.contains(why), "{spec:?}");
    }
    assert!(parse("0 0 31 2 *").unwrap_err().0.contains("within a year"), "an unsatisfiable cron errors rather than looping");
}

// -- next_due -------------------------------------------------------------------------------

#[test]
fn a_daily_cron_lands_on_the_next_occurrence() {
    assert_eq!(parse("0 4 * * *").unwrap().next_due(at(2026, 8, 13, 5, 0)).unwrap(), at(2026, 8, 14, 4, 0));
    assert_eq!(parse("30 5 * * *").unwrap().next_due(at(2026, 8, 13, 4, 15)).unwrap(), at(2026, 8, 13, 5, 30));
    let now = at(2026, 8, 13, 4, 0);
    assert!(parse("0 4 * * *").unwrap().next_due(now).unwrap() > now, "a fire cannot refire itself");
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
    assert_eq!(parse("0 0 10 * *").unwrap().next_due(sat).unwrap(), at(2026, 11, 10, 0, 0));
    assert_eq!(parse("0 0 * * 1").unwrap().next_due(sat).unwrap(), at(2026, 11, 9, 0, 0));
    // `*/1` is not the literal `*`, so the day field counts as restricted, as Python reads it.
    assert_eq!(parse("0 3 */1 * 0").unwrap().next_due(sat).unwrap(), at(2026, 11, 7, 3, 0));
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
    assert_eq!(args["last_slot"], awm_plugin_dvc::iso(last.floor()));

    let never = row(json!({"name": WORKSPACE_BACKUP, "enabled": 1, "catchup_window_s": 3600, "last_fire_at": 0.0}));
    let args = clock::schedule_args(spec_for(WORKSPACE_BACKUP).unwrap(), &never).unwrap().unwrap();
    assert_eq!((args["name"].as_str(), args["timer"].as_str()), (Some("dvc-workspace-backup"), Some("*-*-* 05:30:00")), "the default cron");
    assert!(args.get("last_slot").is_none());

    let off = row(json!({"name": CACHE_SYNC, "cron": "0 4 * * *", "enabled": 0, "catchup_window_s": 72000}));
    assert_eq!(clock::schedule_args(spec, &off).unwrap(), None);
    for cron in ["@every 5m", "0 4 1 * 1", "not a cron"] {
        assert!(clock::schedule_args(spec, &row(json!({"name": CACHE_SYNC, "cron": cron, "enabled": 1}))).is_err(), "{cron}");
    }
}

