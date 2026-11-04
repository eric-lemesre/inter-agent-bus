//! Dates of the bus: ISO 8601 text in UTC, in the exact format of Python's
//! `datetime.isoformat()` — microseconds omitted when they are zero
//! (`CONTRAT.md`, § 5). Deadlines are compared as strings, so the format
//! is part of the contract.

use chrono::{DateTime, Datelike, SubsecRound, TimeDelta, Timelike, Utc};

use crate::Error;

/// The current instant at the microsecond, like `datetime.now(timezone.utc)`.
pub fn now() -> DateTime<Utc> {
    Utc::now().trunc_subsecs(6)
}

/// `isoformat()` of an aware UTC datetime.
pub fn iso(instant: DateTime<Utc>) -> String {
    let micros = instant.nanosecond() / 1_000;
    let base = instant.format("%Y-%m-%dT%H:%M:%S");
    if micros == 0 {
        format!("{base}+00:00")
    } else {
        format!("{base}.{micros:06}+00:00")
    }
}

/// `_now()` of store.py.
pub fn now_iso() -> String {
    iso(now())
}

/// `instant + timedelta(seconds=seconds)`, refused outside the years
/// 1–9999 as Python's datetime does.
pub fn shift(instant: DateTime<Utc>, seconds: i64) -> Result<DateTime<Utc>, Error> {
    TimeDelta::try_seconds(seconds)
        .and_then(|delta| instant.checked_add_signed(delta))
        .filter(|shifted| (1..=9999).contains(&shifted.year()))
        .ok_or(Error::DateRange)
}

/// `(now + timedelta(seconds=seconds)).isoformat()`.
pub fn after(seconds: i64) -> Result<String, Error> {
    shift(now(), seconds).map(iso)
}

/// Read a stored date, with or without microseconds.
pub fn parse(text: &str) -> Result<DateTime<Utc>, Error> {
    DateTime::parse_from_rfc3339(text)
        .map(|d| d.with_timezone(&Utc))
        .map_err(|_| Error::BadDate(text.to_owned()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn iso_should_omit_zero_microseconds() {
        let instant = Utc.with_ymd_and_hms(2026, 11, 4, 9, 5, 7).unwrap();
        assert_eq!(iso(instant), "2026-11-04T09:05:07+00:00");
    }

    #[test]
    fn iso_should_write_six_digit_microseconds() {
        let instant = Utc.with_ymd_and_hms(2026, 11, 4, 9, 5, 7).unwrap() + TimeDelta::microseconds(42);
        assert_eq!(iso(instant), "2026-11-04T09:05:07.000042+00:00");
    }

    #[test]
    fn parse_should_read_both_forms() {
        let plain = parse("2026-11-04T09:05:07+00:00").unwrap();
        let fine = parse("2026-11-04T09:05:07.000042+00:00").unwrap();
        assert_eq!(fine - plain, TimeDelta::microseconds(42));
    }

    #[test]
    fn shift_should_refuse_a_year_beyond_9999() {
        assert!(matches!(shift(now(), i64::MAX / 2_000), Err(Error::DateRange)));
    }
}
