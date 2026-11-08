//! `datetime` in UTC as the Python services store it: `isoformat()`, `fromisoformat()` on 3.12,
//! and the float round trips of `persistence.timestamps`.

use crate::pystr::strip;
use std::time::{SystemTime, UNIX_EPOCH};

const US_PER_S: i64 = 1_000_000;
const US_PER_DAY: i64 = 86_400 * US_PER_S;

/// A parsed `datetime`. `offset_us` is `None` for a naive value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DateTime {
    pub year: i32,
    pub month: u32,
    pub day: u32,
    pub hour: u32,
    pub minute: u32,
    pub second: u32,
    pub microsecond: u32,
    pub offset_us: Option<i64>,
}

impl DateTime {
    /// The UTC instant in microseconds since the epoch. A naive value counts as UTC.
    pub fn utc_us(&self) -> i64 {
        let days = days_from_civil(self.year, self.month, self.day);
        let local = days * US_PER_DAY
            + (self.hour as i64 * 3600 + self.minute as i64 * 60 + self.second as i64) * US_PER_S
            + self.microsecond as i64;
        local - self.offset_us.unwrap_or(0)
    }

    /// `dt.isoformat()`.
    pub fn isoformat(&self) -> String {
        let mut out = format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
            self.year, self.month, self.day, self.hour, self.minute, self.second
        );
        if self.microsecond != 0 {
            out.push_str(&format!(".{:06}", self.microsecond));
        }
        if let Some(off) = self.offset_us {
            let sign = if off < 0 { '-' } else { '+' };
            let off = off.abs();
            let (us, s) = (off % US_PER_S, off / US_PER_S);
            let (h, m, s) = (s / 3600, s / 60 % 60, s % 60);
            out.push_str(&format!("{sign}{h:02}:{m:02}"));
            if us != 0 {
                out.push_str(&format!(":{s:02}.{us:06}"));
            } else if s != 0 {
                out.push_str(&format!(":{s:02}"));
            }
        }
        out
    }

    /// The aware UTC value at `us` microseconds since the epoch.
    pub fn from_utc_us(us: i64) -> DateTime {
        let days = us.div_euclid(US_PER_DAY);
        let rem = us.rem_euclid(US_PER_DAY);
        let (year, month, day) = civil_from_days(days);
        let secs = rem / US_PER_S;
        DateTime {
            year,
            month,
            day,
            hour: (secs / 3600) as u32,
            minute: (secs / 60 % 60) as u32,
            second: (secs % 60) as u32,
            microsecond: (rem % US_PER_S) as u32,
            offset_us: Some(0),
        }
    }
}

/// `datetime.fromisoformat(s)` on Python 3.12. `None` where Python raises `ValueError`.
pub fn fromisoformat(s: &str) -> Option<DateTime> {
    if s.chars().count() < 7 {
        return None;
    }
    let buf = s.as_bytes();
    let len = buf.len();
    let sep = find_separator(buf)?;
    let mut d = Fields::default();
    parse_date(buf, sep, &mut d).ok()?;
    let mut tz = None;
    if len > sep {
        let lead = buf[sep];
        let skip = if lead & 0x80 == 0 {
            1
        } else {
            match lead & 0xf0 {
                0xe0 => 3,
                0xf0 => 4,
                _ => 2,
            }
        };
        let start = sep + skip;
        tz = parse_time(buf, start, len.saturating_sub(start), &mut d)?;
    }
    let offset_us = match tz {
        None => None,
        Some((0, _)) => Some(0),
        Some((secs, usec)) => {
            let off = secs as i64 * US_PER_S + usec as i64;
            if off.abs() >= US_PER_DAY {
                return None;
            }
            Some(off)
        }
    };
    let dt = DateTime {
        year: d.year,
        month: d.month as u32,
        day: d.day as u32,
        hour: d.hour as u32,
        minute: d.minute as u32,
        second: d.second as u32,
        microsecond: d.microsecond as u32,
        offset_us,
    };
    valid(&dt).then_some(dt)
}

#[derive(Default)]
struct Fields {
    year: i32,
    month: i32,
    day: i32,
    hour: i32,
    minute: i32,
    second: i32,
    microsecond: i32,
}

/// The byte at `i`, or NUL past the end, as CPython reads its NUL-terminated buffer.
fn at(buf: &[u8], i: usize) -> u8 {
    buf.get(i).copied().unwrap_or(0)
}

fn is_digit(c: u8) -> bool {
    c.is_ascii_digit()
}

fn parse_digits(buf: &[u8], p: usize, var: &mut i32, n: usize) -> Option<usize> {
    for k in 0..n {
        let c = at(buf, p + k);
        if !is_digit(c) {
            return None;
        }
        *var = *var * 10 + (c - b'0') as i32;
    }
    Some(p + n)
}

fn find_separator(buf: &[u8]) -> Option<usize> {
    let len = buf.len();
    if len == 7 {
        return Some(7);
    }
    if at(buf, 4) == b'-' {
        if at(buf, 5) == b'W' {
            if len < 8 {
                return None;
            }
            if len > 8 && at(buf, 8) == b'-' {
                if len == 9 {
                    return None;
                }
                if len > 10 && is_digit(at(buf, 10)) {
                    return Some(8);
                }
                return Some(10);
            }
            return Some(8);
        }
        return Some(10);
    }
    if at(buf, 4) == b'W' {
        let mut idx = 7;
        while idx < len && is_digit(at(buf, idx)) {
            idx += 1;
        }
        if idx < 9 {
            return Some(idx);
        }
        return Some(if idx % 2 == 0 { 7 } else { 8 });
    }
    Some(8)
}

fn parse_date(buf: &[u8], len: usize, d: &mut Fields) -> Result<(), ()> {
    let mut p = parse_digits(buf, 0, &mut d.year, 4).ok_or(())?;
    let uses_separator = at(buf, p) == b'-';
    if uses_separator {
        p += 1;
    }
    if at(buf, p) == b'W' {
        p += 1;
        let (mut week, mut wday) = (0, 0);
        p = parse_digits(buf, p, &mut week, 2).ok_or(())?;
        if p < len {
            if uses_separator {
                let c = at(buf, p);
                p += 1;
                if c != b'-' {
                    return Err(());
                }
            }
            parse_digits(buf, p, &mut wday, 1).ok_or(())?;
        } else {
            wday = 1;
        }
        let (y, m, dd) = iso_to_ymd(d.year, week, wday).ok_or(())?;
        (d.year, d.month, d.day) = (y, m, dd);
        return Ok(());
    }
    p = parse_digits(buf, p, &mut d.month, 2).ok_or(())?;
    if uses_separator {
        let c = at(buf, p);
        p += 1;
        if c != b'-' {
            return Err(());
        }
    }
    parse_digits(buf, p, &mut d.day, 2).ok_or(())?;
    Ok(())
}

/// Returns 0 for the end of input, 1 for trailing text, and an error for a malformed time.
fn parse_hh_mm_ss_ff(buf: &[u8], start: usize, end: usize, v: &mut [i32; 4]) -> Result<i32, ()> {
    *v = [0; 4];
    let mut p = start;
    let mut has_separator = true;
    for (i, field) in v.iter_mut().take(3).enumerate() {
        p = parse_digits(buf, p, field, 2).ok_or(())?;
        let c = at(buf, p);
        p += 1;
        if i == 0 {
            has_separator = c == b':';
        }
        if p >= end {
            return Ok((c != 0) as i32);
        } else if has_separator && c == b':' {
            continue;
        } else if c == b'.' || c == b',' {
            break;
        } else if !has_separator {
            p -= 1;
        } else {
            return Err(());
        }
    }
    let to_parse = end.saturating_sub(p).min(6);
    let mut us = 0;
    p = parse_digits(buf, p, &mut us, to_parse).ok_or(())?;
    if (1..6).contains(&to_parse) {
        us *= [100000, 10000, 1000, 100, 10][to_parse - 1];
    }
    v[3] = us;
    while is_digit(at(buf, p)) {
        p += 1;
    }
    Ok((at(buf, p) != 0) as i32)
}

/// Parses the time into `d` and returns the UTC offset as (seconds, microseconds), if any.
fn parse_time(buf: &[u8], start: usize, len: usize, d: &mut Fields) -> Option<Option<(i32, i32)>> {
    let end = start + len;
    let mut tzpos = start;
    loop {
        if matches!(at(buf, tzpos), b'Z' | b'+' | b'-') {
            break;
        }
        tzpos += 1;
        if tzpos >= end {
            break;
        }
    }
    let mut v = [0; 4];
    let rv = parse_hh_mm_ss_ff(buf, start, tzpos, &mut v).ok()?;
    [d.hour, d.minute, d.second, d.microsecond] = v;
    if tzpos == end {
        return if rv == 1 { None } else { Some(None) };
    }
    if at(buf, tzpos) == b'Z' {
        return if at(buf, tzpos + 1) != 0 { None } else { Some(Some((0, 0))) };
    }
    let sign = if at(buf, tzpos) == b'-' { -1 } else { 1 };
    let mut t = [0; 4];
    let rv = parse_hh_mm_ss_ff(buf, tzpos + 1, end, &mut t).ok()?;
    if rv != 0 {
        return None;
    }
    Some(Some((sign * (t[0] * 3600 + t[1] * 60 + t[2]), sign * t[3])))
}

fn is_leap(y: i32) -> bool {
    y % 4 == 0 && (y % 100 != 0 || y % 400 == 0)
}

fn days_in_month(y: i32, m: u32) -> u32 {
    match m {
        2 if is_leap(y) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn valid(d: &DateTime) -> bool {
    (1..=9999).contains(&d.year)
        && (1..=12).contains(&d.month)
        && d.day >= 1
        && d.day <= days_in_month(d.year, d.month)
        && d.hour < 24
        && d.minute < 60
        && d.second < 60
        && d.microsecond < 1_000_000
}

/// Monday 0 .. Sunday 6.
fn weekday(y: i32, m: u32, d: u32) -> i64 {
    (days_from_civil(y, m, d) + 3).rem_euclid(7)
}

fn iso_to_ymd(iso_year: i32, week: i32, wday: i32) -> Option<(i32, i32, i32)> {
    if !(1..=9999).contains(&iso_year) {
        return None;
    }
    if week <= 0 || week >= 53 {
        let first = weekday(iso_year, 1, 1);
        if !(week == 53 && (first == 3 || (first == 2 && is_leap(iso_year)))) {
            return None;
        }
    }
    if wday <= 0 || wday >= 8 {
        return None;
    }
    let first_day = days_from_civil(iso_year, 1, 1);
    let first_weekday = (first_day + 3).rem_euclid(7);
    let mut monday = first_day - first_weekday;
    if first_weekday > 3 {
        monday += 7;
    }
    let (y, m, d) = civil_from_days(monday + (week as i64 - 1) * 7 + wday as i64 - 1);
    Some((y, m as i32, d as i32))
}

/// Days since 1970-01-01 of a proleptic Gregorian date.
fn days_from_civil(y: i32, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y as i64 - 1 } else { y as i64 };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m as i64 + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn civil_from_days(z: i64) -> (i32, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = yoe + era * 400 + (m <= 2) as i64;
    (y as i32, m, d)
}

/// `datetime.fromtimestamp(t, timezone.utc)` for a float `t`, rounding to microseconds
/// half to even as CPython's `_PyTime_ObjectToTimeval` does.
pub fn from_timestamp(t: f64) -> Option<DateTime> {
    let intpart = t.trunc();
    let mut frac = round_half_even((t - intpart) * 1e6);
    let mut secs = intpart;
    if frac >= 1e6 {
        frac -= 1e6;
        secs += 1.0;
    } else if frac < 0.0 {
        frac += 1e6;
        secs -= 1.0;
    }
    let us = (secs as i64).checked_mul(US_PER_S)?.checked_add(frac as i64)?;
    let dt = DateTime::from_utc_us(us);
    (1..=9999).contains(&dt.year).then_some(dt)
}

fn round_half_even(x: f64) -> f64 {
    let rounded = x.round();
    if (x - rounded).abs() == 0.5 {
        2.0 * (x / 2.0).round()
    } else {
        rounded
    }
}

/// `int(dt.timestamp() * 1000)`. Exact for instants below 2**53 microseconds (year 2255).
pub fn timestamp_ms(utc_us: i64) -> i64 {
    ((utc_us as f64 / 1e6) * 1000.0) as i64
}

/// `persistence.timestamps.ms_to_iso`: `fromtimestamp(ms / 1000, utc).isoformat()`.
pub fn ms_to_iso(ms: i64) -> Option<String> {
    from_timestamp(ms as f64 / 1000.0).map(|dt| dt.isoformat())
}

/// `persistence.timestamps.iso_to_ms`: strip, `Z` as `+00:00`, a naive value as UTC.
pub fn iso_to_ms(s: &str) -> Option<i64> {
    let s = strip(s);
    if s.is_empty() {
        return None;
    }
    fromisoformat(&s.replace('Z', "+00:00")).map(|dt| timestamp_ms(dt.utc_us()))
}

/// `persistence.timestamps.ms_to_date`: the UTC date of `ms / 1000` as `YYYY-MM-DD`.
pub fn ms_to_date(ms: i64) -> Option<String> {
    from_timestamp(ms as f64 / 1000.0).map(|dt| date_iso(&dt))
}

/// `dt.date().isoformat()`.
pub fn date_iso(dt: &DateTime) -> String {
    format!("{:04}-{:02}-{:02}", dt.year, dt.month, dt.day)
}

/// `dt.strftime("%Y-%m-%dT%H:%M:%SZ")` of a UTC value, with glibc's unpadded `%Y`.
pub fn zulu(dt: &DateTime) -> String {
    format!("{}-{:02}-{:02}T{:02}:{:02}:{:02}Z", dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
}

/// Microseconds since the epoch now, as `datetime.now(timezone.utc)` reads the clock.
pub fn now_us() -> i64 {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    d.as_secs() as i64 * US_PER_S + d.subsec_micros() as i64
}

/// `datetime.now(timezone.utc).isoformat()`.
pub fn now_iso() -> String {
    DateTime::from_utc_us(now_us()).isoformat()
}

/// `persistence.timestamps.now_ms`.
pub fn now_ms() -> i64 {
    timestamp_ms(now_us())
}
