//! The jobs' 5-field cron (`min hour dom month dow`, local time), parsed as Python's
//! `awm.config.cron` did, and converted to the systemd OnCalendar that routine's timer
//! fires on.
//!
//! Cron ORs day-of-month and day-of-week when both are restricted. OnCalendar ANDs them, so
//! a spec that restricts both to a real subset is refused. `@every` has no OnCalendar form
//! and is refused too.

use std::collections::BTreeSet;

use chrono::{Datelike, Local, TimeZone, Timelike};

#[derive(Debug, thiserror::Error, PartialEq)]
#[error("{0}")]
pub struct CronError(pub String);

const BOUNDS: [(u32, u32); 5] = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)];
const DOW: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

#[derive(Debug, Clone, PartialEq)]
pub struct Cron {
    pub sets: [BTreeSet<u32>; 5],
    dom_restricted: bool,
    dow_restricted: bool,
}

fn is_digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

fn parse_field(field: &str, low: u32, high: u32) -> Result<BTreeSet<u32>, CronError> {
    let mut out = BTreeSet::new();
    for part in field.split(',') {
        let mut part = part.trim();
        if part.is_empty() {
            return Err(CronError(format!("empty cron field component in '{field}'")));
        }
        let mut step = 1;
        if let Some((base, s)) = part.split_once('/') {
            if !is_digits(s) || s.parse::<u32>().unwrap_or(0) == 0 {
                return Err(CronError(format!("bad step in '{part}'")));
            }
            step = s.parse().unwrap_or(1);
            part = base;
        }
        let (lo, hi) = if part == "*" {
            (low, high)
        } else if let Some((a, b)) = part.split_once('-') {
            if !(is_digits(a) && is_digits(b)) {
                return Err(CronError(format!("bad range in '{part}'")));
            }
            (a.parse().unwrap_or(u32::MAX), b.parse().unwrap_or(u32::MAX))
        } else if is_digits(part) {
            let v = part.parse().unwrap_or(u32::MAX);
            (v, v)
        } else {
            return Err(CronError(format!("unrecognised cron token '{part}'")));
        };
        if lo > hi || lo < low || hi > high {
            return Err(CronError(format!("cron value out of [{low},{high}] range: '{part}'")));
        }
        out.extend((lo..=hi).step_by(step as usize));
    }
    Ok(out)
}

pub fn parse(spec: &str) -> Result<Cron, CronError> {
    let spec = spec.trim();
    if spec.is_empty() {
        return Err(CronError("empty schedule spec".into()));
    }
    if spec.starts_with('@') {
        return Err(CronError(format!(
            "'{spec}' is an interval: routine's timers fire on a calendar, so give a 5-field cron (min hour dom month dow)"
        )));
    }
    let fields: Vec<&str> = spec.split_whitespace().collect();
    if fields.len() != 5 {
        return Err(CronError(format!("cron must have 5 fields (min hour dom month dow), got {}: '{spec}'", fields.len())));
    }
    let mut sets: [BTreeSet<u32>; 5] = Default::default();
    for (i, f) in fields.iter().enumerate() {
        sets[i] = parse_field(f, BOUNDS[i].0, BOUNDS[i].1)?;
    }
    let c = Cron { sets, dom_restricted: fields[2] != "*", dow_restricted: fields[4] != "*" };
    c.next_due(0.0)?;
    Ok(c)
}

fn full(set: &BTreeSet<u32>, i: usize) -> bool {
    set.len() as u32 == BOUNDS[i].1 - BOUNDS[i].0 + 1
}

fn list(set: &BTreeSet<u32>, i: usize, width: usize) -> String {
    if full(set, i) {
        return "*".into();
    }
    set.iter().map(|v| format!("{v:0width$}")).collect::<Vec<_>>().join(",")
}

impl Cron {
    fn matches(&self, dt: &chrono::DateTime<Local>) -> bool {
        let [minute, hour, dom, month, dow] = &self.sets;
        let cron_dow = dt.weekday().num_days_from_sunday();
        if !minute.contains(&dt.minute()) || !hour.contains(&dt.hour()) || !month.contains(&dt.month()) {
            return false;
        }
        match (self.dom_restricted, self.dow_restricted) {
            (true, true) => dom.contains(&dt.day()) || dow.contains(&cron_dow),
            (true, false) => dom.contains(&dt.day()),
            (false, true) => dow.contains(&cron_dow),
            (false, false) => true,
        }
    }

    /// The next epoch second strictly after `after` that the spec matches, in local time.
    pub fn next_due(&self, after: f64) -> Result<f64, CronError> {
        let start = Local.timestamp_opt(after.floor() as i64, 0).single().ok_or_else(|| CronError("bad time".into()))?;
        let mut cur = start.with_second(0).unwrap_or(start) + chrono::Duration::minutes(1);
        for _ in 0..366 * 24 * 60 {
            if self.matches(&cur) {
                return Ok(cur.timestamp() as f64);
            }
            cur += chrono::Duration::minutes(1);
        }
        Err(CronError("no matching time within a year for this cron".into()))
    }

    /// The same schedule as a systemd OnCalendar expression.
    pub fn on_calendar(&self) -> Result<String, CronError> {
        let [minute, hour, dom, month, dow] = &self.sets;
        let (dom_s, dow_s) = match (self.dom_restricted, self.dow_restricted) {
            (true, true) if full(dom, 2) || full(dow, 4) => ("*".to_string(), String::new()),
            (true, true) => {
                return Err(CronError(
                    "a cron that restricts both day-of-month and day-of-week fires on either; a systemd timer needs both, so pick one".into(),
                ))
            }
            (true, false) => (list(dom, 2, 2), String::new()),
            (false, true) => ("*".to_string(), if full(dow, 4) { String::new() } else { dow.iter().map(|d| DOW[*d as usize]).collect::<Vec<_>>().join(",") }),
            (false, false) => ("*".to_string(), String::new()),
        };
        let date = format!("*-{}-{dom_s}", list(month, 3, 2));
        let time = format!("{}:{}:00", list(hour, 1, 2), list(minute, 0, 2));
        Ok(if dow_s.is_empty() { format!("{date} {time}") } else { format!("{dow_s} {date} {time}") })
    }
}

/// Parse, and convert to OnCalendar.
pub fn to_on_calendar(spec: &str) -> Result<String, CronError> {
    parse(spec)?.on_calendar()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn systemd_calendars_come_out_right_and_what_systemd_cannot_say_is_refused() {
        assert_eq!(to_on_calendar("0 4 * * *").unwrap(), "*-*-* 04:00:00");
        assert_eq!(to_on_calendar("*/15 9-11 * * 1-5").unwrap(), "Mon,Tue,Wed,Thu,Fri *-*-* 09,10,11:00,15,30,45:00");
        assert_eq!(to_on_calendar("0 0 1,15 */6 *").unwrap(), "*-01,07-01,15 00:00:00");
        assert_eq!(to_on_calendar("0 3 */1 * 0").unwrap(), "*-*-* 03:00:00");
        assert!(to_on_calendar("@every 30s").unwrap_err().0.contains("interval"));
        assert!(to_on_calendar("0 4 1 * 1").unwrap_err().0.contains("both"));
        assert!(to_on_calendar("0 4 * *").is_err());
        assert!(to_on_calendar("61 4 * * *").is_err());
        assert!(to_on_calendar("0 4 * * 7").is_err());
    }
}
