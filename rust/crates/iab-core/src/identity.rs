//! Who the caller is and which database is the rendezvous: identity
//! `harness@workspace`, project key and database resolution, identical to
//! store.py (`CONTRAT.md`, §§ 1 and 7) — two implementations launched in
//! the same folder must name the same agent and open the same file.

use std::path::{Component, Path, PathBuf};

use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::pytext::{fold, strip};

/// `[a-z0-9][a-z0-9._-]{0,39}`.
fn is_component(text: &str) -> bool {
    let bytes = text.as_bytes();
    let class = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit();
    match bytes.split_first() {
        Some((&first, rest)) => {
            class(first)
                && rest.len() <= 39
                && rest.iter().all(|&b| class(b) || matches!(b, b'.' | b'_' | b'-'))
        }
        None => false,
    }
}

/// A valid agent name: `harness` or `harness@workspace`.
pub fn is_valid_name(name: &str) -> bool {
    match name.split_once('@') {
        Some((harness, workspace)) => is_component(harness) && is_component(workspace),
        None => is_component(name),
    }
}

/// `harness@workspace` → (harness, workspace); a bare name → (name, "").
pub fn split_name(name: &str) -> (String, String) {
    let name = fold(name);
    match name.split_once('@') {
        Some((harness, workspace)) => (harness.to_owned(), workspace.to_owned()),
        None => (name, String::new()),
    }
}

/// The workspace of a machine's general queue, `<machine>@general`: tasks
/// asking for an action specific to that machine, taken by one of the
/// agents watching it.
pub const MACHINE_QUEUE_WORKSPACE: &str = "general";

/// `odin` → `odin@general`, the general queue of that machine.
pub fn machine_queue(machine: &str) -> String {
    format!("{}@{MACHINE_QUEUE_WORKSPACE}", slugify_component(machine))
}

/// Whether a name designates a machine's general queue.
pub fn is_machine_queue(name: &str) -> bool {
    let (harness, workspace) = split_name(name);
    !harness.is_empty() && workspace == MACHINE_QUEUE_WORKSPACE
}

/// Fold a folder or client name into one identity component: lower case,
/// every run of characters outside `[a-z0-9._-]` replaced by `-`, leading
/// punctuation dropped, 40 characters at most, `root` when nothing remains.
pub fn slugify_component(text: &str) -> String {
    let mut slug = String::new();
    let mut in_run = false;
    for c in text.to_lowercase().chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '_' | '-') {
            slug.push(c);
            in_run = false;
        } else if !in_run {
            slug.push('-');
            in_run = true;
        }
    }
    let slug = slug.trim_start_matches(['.', '_', '-']).trim_end_matches('-');
    let cut: String = slug.chars().take(40).collect();
    let cut = cut.trim_end_matches('-');
    if cut.is_empty() {
        "root".to_owned()
    } else {
        cut.to_owned()
    }
}

/// The user's home directory, as Python's `Path.home()` finds it.
fn home() -> PathBuf {
    std::env::home_dir().unwrap_or_default()
}

/// Platform data directory of the bus.
pub fn data_dir() -> PathBuf {
    let base = if cfg!(windows) {
        std::env::var_os("LOCALAPPDATA").map_or_else(|| home().join("AppData/Local"), PathBuf::from)
    } else if cfg!(target_os = "macos") {
        home().join("Library/Application Support")
    } else {
        std::env::var_os("XDG_DATA_HOME").map_or_else(|| home().join(".local/share"), PathBuf::from)
    };
    base.join("inter-agent-bus")
}

/// Lexical normalization: `.` dropped, `..` pops the previous component.
fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// `os.path.realpath`: links resolved, absolute, never the `\\?\` form on
/// Windows. A path that does not exist (an alias of `workspaces.json`)
/// resolves its longest existing prefix and keeps the rest as written.
pub fn realpath(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    let absolute = normalize(&absolute);
    let mut existing = absolute.as_path();
    let mut rest = Vec::new();
    loop {
        if let Ok(resolved) = dunce::canonicalize(existing) {
            let mut out = resolved;
            for part in rest.iter().rev() {
                out.push(part);
            }
            return out;
        }
        match (existing.file_name(), existing.parent()) {
            (Some(name), Some(parent)) => {
                rest.push(name.to_os_string());
                existing = parent;
            }
            _ => return absolute,
        }
    }
}

fn current_realpath() -> PathBuf {
    realpath(&std::env::current_dir().unwrap_or_default())
}

/// Path comparison key: case-insensitive on Windows, like Python's
/// `PureWindowsPath`.
fn key(path: &Path) -> Vec<String> {
    path.components()
        .map(|c| {
            let text = c.as_os_str().to_string_lossy();
            if cfg!(windows) {
                text.to_lowercase()
            } else {
                text.into_owned()
            }
        })
        .collect()
}

/// `inner` is `outer` or lies below it.
fn within(inner: &Path, outer: &Path) -> bool {
    key(inner).starts_with(&key(outer))
}

fn expand_user(text: &str) -> PathBuf {
    match text.strip_prefix('~') {
        Some("") => home(),
        Some(rest) if rest.starts_with(['/', '\\']) => home().join(&rest[1..]),
        _ => PathBuf::from(text),
    }
}

/// Operator table {path: workspace} (`IAB_WORKSPACES`, else the data
/// directory's `workspaces.json`), keys resolved; unreadable or malformed
/// → empty.
fn workspace_aliases() -> Vec<(String, String)> {
    let file = std::env::var("IAB_WORKSPACES")
        .ok()
        .filter(|v| !v.is_empty())
        .map_or_else(|| data_dir().join("workspaces.json"), PathBuf::from);
    let Ok(text) = std::fs::read_to_string(file) else {
        return Vec::new();
    };
    let Ok(serde_json::Value::Object(raw)) = serde_json::from_str::<serde_json::Value>(&text) else {
        return Vec::new();
    };
    let mut aliases: Vec<(String, String)> = Vec::new();
    for (path, value) in raw {
        let serde_json::Value::String(value) = value else {
            continue;
        };
        if strip(&value).is_empty() {
            continue;
        }
        let prefix = realpath(&expand_user(&path)).to_string_lossy().into_owned();
        let slug = slugify_component(&value);
        match aliases.iter_mut().find(|(p, _)| *p == prefix) {
            Some(entry) => entry.1 = slug,
            None => aliases.push((prefix, slug)),
        }
    }
    aliases
}

/// Workspace slug of a launch directory and how it was found: `alias`
/// (operator table, longest matching prefix, never past a repository
/// boundary), `git` (first ancestor holding a `.git`) or `cwd`.
pub fn derive_workspace(real: &Path) -> (String, &'static str) {
    let git_top = real.ancestors().find(|c| c.join(".git").exists());
    let mut best: Option<(usize, String)> = None;
    for (prefix, slug) in workspace_aliases() {
        let pre = Path::new(&prefix);
        if !within(real, pre) {
            continue;
        }
        if let Some(top) = git_top {
            if !within(pre, top) {
                continue; // the repository lies below the alias: it wins
            }
        }
        let length = prefix.chars().count();
        if best.as_ref().is_none_or(|(l, _)| length > *l) {
            best = Some((length, slug));
        }
    }
    if let Some((_, slug)) = best {
        return (slug, "alias");
    }
    if let Some(top) = git_top {
        return (slugify_component(&file_name(top)), "git");
    }
    (slugify_component(&file_name(real)), "cwd")
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// The caller's identity, from most explicit to most inferred.
#[derive(Debug, Clone, Serialize)]
pub struct Identity {
    /// `env`, or `none` without a harness.
    pub source: &'static str,
    /// `harness@workspace`, or the bare harness.
    pub agent_name: Option<String>,
    /// Client carrying the model.
    pub harness: Option<String>,
    /// Repository or folder worked in.
    pub workspace: Option<String>,
    /// `name`, `env`, `alias`, `git` or `cwd`.
    pub workspace_source: Option<&'static str>,
    /// Real launch directory.
    pub cwd: String,
    /// Declared model (`IAB_MODEL`).
    pub model: Option<String>,
}

fn env_text(name: &str) -> String {
    std::env::var(name).unwrap_or_default()
}

/// `resolve_identity()` of store.py.
pub fn resolve_identity() -> Identity {
    let real = current_realpath();
    let cwd = real.to_string_lossy().into_owned();
    let model = Some(strip(&env_text("IAB_MODEL")).to_owned()).filter(|m| !m.is_empty());
    let forced = Some(env_text("IAB_AGENT_NAME"))
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| env_text("ORCHESTRATOR_AGENT_NAME"));
    let forced = fold(&forced);
    if forced.is_empty() {
        return Identity {
            source: "none",
            agent_name: None,
            harness: None,
            workspace: None,
            workspace_source: None,
            cwd,
            model,
        };
    }
    let (harness, workspace, source) = if forced.contains('@') {
        let (harness, workspace) = split_name(&forced);
        (harness, workspace, "name")
    } else {
        let workspace = fold(&env_text("IAB_WORKSPACE"));
        if workspace.is_empty() {
            let (workspace, source) = derive_workspace(&real);
            (forced, workspace, source)
        } else {
            (forced, slugify_component(&workspace), "env")
        }
    };
    let name = if workspace.is_empty() {
        harness.clone()
    } else {
        format!("{harness}@{workspace}")
    };
    Identity {
        source: "env",
        agent_name: Some(name),
        harness: Some(harness),
        workspace: Some(workspace),
        workspace_source: Some(source),
        cwd,
        model,
    }
}

/// Stable per-project key of a directory: real path, case-folded on
/// Windows and macOS, then `<slug>-<12 hex of its SHA-256>`.
pub fn project_key_of(real: &Path) -> String {
    let text = real.to_string_lossy();
    let normalized = if cfg!(any(windows, target_os = "macos")) {
        text.to_lowercase()
    } else {
        text.into_owned()
    };
    let digest = Sha256::digest(normalized.as_bytes());
    let hex: String = digest.iter().take(6).map(|b| format!("{b:02x}")).collect();
    let mut slug = String::new();
    let mut in_run = false;
    for c in file_name(real).chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '_' | '-') {
            slug.push(c);
            in_run = false;
        } else if !in_run {
            slug.push('-');
            in_run = true;
        }
    }
    let slug: String = slug.chars().take(40).collect();
    let slug = if slug.is_empty() { "root".to_owned() } else { slug };
    format!("{slug}-{hex}")
}

/// Project key of the launch directory.
pub fn project_key() -> String {
    project_key_of(&current_realpath())
}

/// A database target: a file, or a PostgreSQL URL.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    /// SQLite file.
    File(PathBuf),
    /// `postgresql://` or `postgres://` URL in libpq format.
    Url(String),
}

/// `dialect.is_url`.
pub fn is_url(text: &str) -> bool {
    text.starts_with("postgresql://") || text.starts_with("postgres://")
}

/// The bus database and how it was chosen (`_resolve_db` of store.py).
pub fn resolve_db() -> (Target, String) {
    for var in ["IAB_DB", "ORCHESTRATOR_DB"] {
        let value = env_text(var);
        if !value.is_empty() {
            let target = if is_url(&value) {
                Target::Url(value)
            } else {
                Target::File(PathBuf::from(value))
            };
            return (target, format!("env:{var}"));
        }
    }
    let global = data_dir().join("bus.db");
    if global.exists() {
        return (
            Target::File(global),
            "global (pre-isolation bus kept — set IAB_DB to opt out)".to_owned(),
        );
    }
    let legacy = home().join(".local/share/multi-agent-orchestrator/orchestrator.db");
    if legacy.exists() {
        return (
            Target::File(legacy),
            "legacy (pre-rename bus kept — set IAB_DB to opt out)".to_owned(),
        );
    }
    let file = data_dir().join("projects").join(format!("{}.db", project_key()));
    (Target::File(file), "project".to_owned())
}

/// A target fit for display: a URL rebuilt from its scheme, user, host,
/// port and database only — no password, no query string.
pub fn describe(target: &Target) -> String {
    match target {
        Target::File(path) => path.display().to_string(),
        Target::Url(url) => describe_url(url),
    }
}

fn describe_url(url: &str) -> String {
    let Some((scheme, rest)) = url.split_once("://") else {
        return String::new();
    };
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (netloc, tail) = rest.split_at(end);
    let path = &tail[..tail.find(['?', '#']).unwrap_or(tail.len())];
    let (userinfo, hostport) = match netloc.rsplit_once('@') {
        Some((userinfo, hostport)) => (Some(userinfo), hostport),
        None => (None, netloc),
    };
    let user = userinfo
        .map(|u| u.split_once(':').map_or(u, |(name, _)| name))
        .filter(|u| !u.is_empty())
        .map_or(String::new(), |u| format!("{u}@"));
    let (host, port) = if let Some(bracketed) = hostport.strip_prefix('[') {
        match bracketed.split_once(']') {
            Some((host, after)) => (host, after.strip_prefix(':').unwrap_or("")),
            None => (bracketed, ""),
        }
    } else {
        match hostport.rsplit_once(':') {
            Some((host, port)) => (host, port),
            None => (hostport, ""),
        }
    };
    let port = port
        .parse::<u16>()
        .ok()
        .filter(|p| *p != 0)
        .map_or(String::new(), |p| format!(":{p}"));
    format!("{scheme}://{user}{}{port}{path}", host.to_lowercase())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugify_should_turn_accents_into_dashes() {
        assert_eq!(slugify_component("Dépôt"), "d-p-t");
    }

    #[test]
    fn slugify_should_drop_leading_punctuation_and_trailing_dashes() {
        assert_eq!(slugify_component("._Mon Projet !"), "mon-projet");
    }

    #[test]
    fn slugify_should_fall_back_to_root() {
        assert_eq!(slugify_component("!!!"), "root");
    }

    #[test]
    fn valid_names_should_follow_the_component_class() {
        assert!(is_valid_name("claude@homelab"));
        assert!(is_valid_name("kimi"));
        assert!(!is_valid_name("Claude@homelab"));
        assert!(!is_valid_name("a@b@c"));
        assert!(!is_valid_name("-a"));
    }

    #[test]
    fn project_key_should_match_the_python_value() {
        // Value computed by store.project_key on Linux.
        if cfg!(any(windows, target_os = "macos")) {
            return; // the path is case-folded there
        }
        assert_eq!(
            project_key_of(Path::new("/srv/Dépôt d'essai")),
            "D-p-t-d-essai-b52f8f5d4e5b"
        );
    }

    #[test]
    fn describe_should_hide_password_and_parameters() {
        let url = Target::Url("postgresql://iab:secret@DB.example:5433/bus?sslmode=verify-full".into());
        assert_eq!(describe(&url), "postgresql://iab@db.example:5433/bus");
    }
}
