//! TLS of a PostgreSQL bus: libpq's `ssl*` parameters turned into a rustls
//! configuration.
//!
//! - `verify-full`: chain and host name checked — what a remote bus uses;
//! - `verify-ca`: chain checked, host name not;
//! - `require`: encrypted; checked like `verify-ca` when a root file is
//!   given or present at libpq's default place, as libpq does;
//! - `disable`: clear text, explicit;
//! - `prefer`, `allow` and no `sslmode` at all (libpq's default is
//!   `prefer`) are refused over TCP: they fall back to clear text when the
//!   server offers no TLS.
//!
//! Roots: `sslrootcert=<file>`, or libpq's default `root.crt` when it
//! exists, else the system store (`sslrootcert=system`) — on Windows the
//! machine's store, where a company's internal authority already lives.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use rustls::client::WebPkiServerVerifier;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{CryptoProvider, verify_tls12_signature, verify_tls13_signature};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use rustls::{CertificateError, ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme};
use tokio_postgres_rustls::MakeRustlsConnect;

use crate::DbError;

/// Where the trusted roots come from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Roots {
    /// The operating system's store.
    System,
    /// A PEM file.
    File(PathBuf),
}

/// What the server certificate must prove.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Check {
    /// Chain and host name.
    Full,
    /// Chain only.
    Chain,
    /// Nothing (`sslmode=require` without a root file).
    Encryption,
}

/// TLS settings read from the URL and the environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TlsSettings {
    /// Server certificate check.
    pub check: Check,
    /// Trusted roots.
    pub roots: Roots,
    /// Client certificate and key.
    pub client: Option<(PathBuf, PathBuf)>,
    /// TLS 1.3 only (`ssl_min_protocol_version=TLSv1.3`).
    pub tls13_only: bool,
}

fn libpq_dir() -> Option<PathBuf> {
    if cfg!(windows) {
        std::env::var_os("APPDATA").map(|d| PathBuf::from(d).join("postgresql"))
    } else {
        std::env::home_dir().map(|h| h.join(".postgresql"))
    }
}

fn default_file(name: &str) -> Option<PathBuf> {
    libpq_dir().map(|d| d.join(name)).filter(|p| p.is_file())
}

/// The TLS settings of a connection, `None` for clear text.
pub fn settings(ssl: &HashMap<String, String>, unix_socket: bool) -> Result<Option<TlsSettings>, DbError> {
    let param = |key: &str, env: &str| -> Option<String> {
        ssl.get(key)
            .cloned()
            .or_else(|| std::env::var(env).ok())
            .filter(|v| !v.is_empty())
    };
    let mode = param("sslmode", "PGSSLMODE").unwrap_or_else(|| "prefer".to_owned());
    if unix_socket {
        return Ok(None); // libpq ignores sslmode on a Unix socket
    }
    let check = match mode.as_str() {
        "disable" => return Ok(None),
        "allow" | "prefer" => {
            return Err(DbError::Config(format!(
                "sslmode={mode} falls back to clear text when the server offers no TLS — state \
                 sslmode=verify-full (or verify-ca, require, disable) in the URL"
            )));
        }
        "require" => Check::Encryption,
        "verify-ca" => Check::Chain,
        "verify-full" => Check::Full,
        other => return Err(DbError::Config(format!("unknown sslmode '{other}'"))),
    };
    for unsupported in ["sslpassword", "sslcrl", "sslcrldir"] {
        if ssl.get(unsupported).is_some_and(|v| !v.is_empty()) {
            return Err(DbError::Config(format!(
                "{unsupported} is not supported by the native client"
            )));
        }
    }
    let tls13_only = match ssl.get("ssl_min_protocol_version").map(String::as_str) {
        None | Some("" | "TLSv1" | "TLSv1.1" | "TLSv1.2") => false,
        Some("TLSv1.3") => true,
        Some(other) => {
            return Err(DbError::Config(format!(
                "unknown ssl_min_protocol_version '{other}'"
            )));
        }
    };
    let explicit_root = param("sslrootcert", "PGSSLROOTCERT");
    let roots = match explicit_root.as_deref() {
        Some("system") => Roots::System,
        Some(path) => Roots::File(PathBuf::from(path)),
        None => default_file("root.crt").map_or(Roots::System, Roots::File),
    };
    let check = match (check, &roots) {
        (Check::Encryption, Roots::File(_)) => Check::Chain,
        (check, _) => check,
    };
    let cert = param("sslcert", "PGSSLCERT")
        .map(PathBuf::from)
        .or_else(|| default_file("postgresql.crt"));
    let key = param("sslkey", "PGSSLKEY")
        .map(PathBuf::from)
        .or_else(|| default_file("postgresql.key"));
    let client = match (cert, key) {
        (Some(cert), Some(key)) => Some((cert, key)),
        (None, _) => None,
        (Some(cert), None) => {
            return Err(DbError::Config(format!(
                "client certificate {} given without sslkey",
                cert.display()
            )));
        }
    };
    Ok(Some(TlsSettings {
        check,
        roots,
        client,
        tls13_only,
    }))
}

fn tls_error(what: &str, path: &Path, error: impl std::fmt::Display) -> DbError {
    DbError::Config(format!("{what} {}: {error}", path.display()))
}

/// Accepts a certificate valid for another name: `verify-ca`.
#[derive(Debug)]
struct ChainOnly(Arc<dyn ServerCertVerifier>);

impl ServerCertVerifier for ChainOnly {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        match self
            .0
            .verify_server_cert(end_entity, intermediates, server_name, ocsp_response, now)
        {
            Err(rustls::Error::InvalidCertificate(
                CertificateError::NotValidForName | CertificateError::NotValidForNameContext { .. },
            )) => Ok(ServerCertVerified::assertion()),
            other => other,
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        self.0.verify_tls12_signature(message, cert, dss)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        self.0.verify_tls13_signature(message, cert, dss)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.supported_verify_schemes()
    }
}

/// Encryption without authentication of the server: `sslmode=require`
/// without a root file, as libpq does. The handshake signatures are still
/// checked.
#[derive(Debug)]
struct EncryptionOnly(Arc<CryptoProvider>);

impl ServerCertVerifier for EncryptionOnly {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

fn verifier(
    settings: &TlsSettings,
    provider: &Arc<CryptoProvider>,
) -> Result<Arc<dyn ServerCertVerifier>, DbError> {
    if settings.check == Check::Encryption {
        return Ok(Arc::new(EncryptionOnly(Arc::clone(provider))));
    }
    let full: Arc<dyn ServerCertVerifier> = match &settings.roots {
        Roots::System => Arc::new(
            rustls_platform_verifier::Verifier::new(Arc::clone(provider))
                .map_err(|e| DbError::Config(format!("system certificate store: {e}")))?,
        ),
        Roots::File(path) => {
            let mut roots = RootCertStore::empty();
            for cert in CertificateDer::pem_file_iter(path).map_err(|e| tls_error("cannot read", path, e))? {
                let cert = cert.map_err(|e| tls_error("cannot read", path, e))?;
                roots
                    .add(cert)
                    .map_err(|e| tls_error("invalid root certificate in", path, e))?;
            }
            if roots.is_empty() {
                return Err(tls_error("no certificate in", path, "empty file"));
            }
            WebPkiServerVerifier::builder_with_provider(Arc::new(roots), Arc::clone(provider))
                .build()
                .map_err(|e| tls_error("unusable roots in", path, e))?
        }
    };
    Ok(match settings.check {
        Check::Chain => Arc::new(ChainOnly(full)),
        Check::Full | Check::Encryption => full,
    })
}

/// The rustls connector of a TLS bus.
pub fn connector(settings: &TlsSettings) -> Result<MakeRustlsConnect, DbError> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let versions: &[&rustls::SupportedProtocolVersion] = if settings.tls13_only {
        &[&rustls::version::TLS13]
    } else {
        rustls::DEFAULT_VERSIONS
    };
    let builder = ClientConfig::builder_with_provider(Arc::clone(&provider))
        .with_protocol_versions(versions)
        .map_err(|e| DbError::Config(format!("TLS: {e}")))?
        .dangerous()
        .with_custom_certificate_verifier(verifier(settings, &provider)?);
    let config = match &settings.client {
        None => builder.with_no_client_auth(),
        Some((cert, key)) => {
            let chain = CertificateDer::pem_file_iter(cert)
                .map_err(|e| tls_error("cannot read", cert, e))?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| tls_error("cannot read", cert, e))?;
            let key =
                PrivateKeyDer::from_pem_file(key).map_err(|e| tls_error("cannot read the key", key, e))?;
            builder
                .with_client_auth_cert(chain, key)
                .map_err(|e| DbError::Config(format!("client certificate: {e}")))?
        }
    };
    Ok(MakeRustlsConnect::new(config))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ssl(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect()
    }

    #[test]
    fn prefer_should_be_refused_over_tcp() {
        assert!(settings(&ssl(&[("sslmode", "prefer")]), false).is_err());
    }

    #[test]
    fn disable_should_mean_clear_text() {
        assert_eq!(settings(&ssl(&[("sslmode", "disable")]), false).unwrap(), None);
    }

    #[test]
    fn a_unix_socket_should_never_use_tls() {
        assert_eq!(settings(&ssl(&[("sslmode", "verify-full")]), true).unwrap(), None);
    }

    #[test]
    fn require_with_a_root_file_should_check_the_chain() {
        let got = settings(
            &ssl(&[("sslmode", "require"), ("sslrootcert", "/x/root.crt")]),
            false,
        )
        .unwrap();
        assert_eq!(got.map(|s| s.check), Some(Check::Chain));
    }

    #[test]
    fn sslrootcert_system_should_use_the_system_store() {
        let got = settings(
            &ssl(&[("sslmode", "verify-full"), ("sslrootcert", "system")]),
            false,
        )
        .unwrap();
        assert_eq!(got.map(|s| s.roots), Some(Roots::System));
    }

    #[test]
    fn an_encrypted_key_should_be_refused() {
        assert!(settings(&ssl(&[("sslmode", "verify-full"), ("sslpassword", "x")]), false).is_err());
    }
}
