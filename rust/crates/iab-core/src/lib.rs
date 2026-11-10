//! State logic of the inter-agent bus — the counterpart of `store.py`.
//!
//! Every operation opens one connection, runs short transactions (the
//! writer lock first, nothing outside the database while it is held) and
//! answers with the text the reference implementation answers: `OK: …`, a
//! JSON document, `NO_TASK`, or `ERROR: …` for a refusal. A refusal is a
//! value, not an error: [`Error`] is reserved to failures (database,
//! malformed stored data).

pub mod clock;
pub mod identity;
pub mod instructions;
pub mod pytext;
mod store;

pub use store::{Bus, db_info};

/// Failures of the core.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// Database failure.
    #[error(transparent)]
    Db(#[from] iab_db::DbError),
    /// JSON serialization or stored JSON that does not parse.
    #[error("JSON: {0}")]
    Json(#[from] serde_json::Error),
    /// A date outside the years 1–9999.
    #[error("date out of range")]
    DateRange,
    /// A stored date that is not ISO 8601.
    #[error("stored date '{0}' is not ISO 8601")]
    BadDate(String),
    /// Something the native client does not do (yet).
    #[error("{0}")]
    Unsupported(String),
}
