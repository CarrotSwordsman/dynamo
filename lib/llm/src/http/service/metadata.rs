// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES and Baseten, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! HTTP header → context metadata extraction.
//!
//! Any request header whose name starts with `DYNAMO_METADATA_HEADER_PREFIX_DEFAULT`
//! (or the value of the `DYN_METADATA_HEADER` env var) is stripped of its prefix
//! and inserted into the [`dynamo_runtime::pipeline::Context`] metadata map.
//!
//! Example: `x-dynamo-meta-tenant: acme` → `metadata["tenant"] = "acme"`.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use axum::http::HeaderMap;

/// Default header prefix for context metadata injected from HTTP request headers.
/// Overridable at startup via the [`DYNAMO_METADATA_HEADER_ENV`] environment variable.
pub const DYNAMO_METADATA_HEADER_PREFIX_DEFAULT: &str = "x-dynamo-meta-";

/// Environment variable that overrides [`DYNAMO_METADATA_HEADER_PREFIX_DEFAULT`].
pub const DYNAMO_METADATA_HEADER_ENV: &str = "DYN_METADATA_HEADER";

static METADATA_HEADER_PREFIX: OnceLock<String> = OnceLock::new();

pub(super) fn metadata_header_prefix() -> &'static str {
    METADATA_HEADER_PREFIX.get_or_init(|| {
        std::env::var(DYNAMO_METADATA_HEADER_ENV)
            .unwrap_or_else(|_| DYNAMO_METADATA_HEADER_PREFIX_DEFAULT.to_string())
    })
}

/// Extract all `<prefix><key>: <value>` headers as a metadata map.
///
/// Headers that are not valid UTF-8 are silently skipped.
pub fn extract_metadata_from_headers(headers: &HeaderMap) -> BTreeMap<String, String> {
    let prefix = metadata_header_prefix();
    headers
        .iter()
        .filter_map(|(name, value)| {
            let key = name.as_str().strip_prefix(prefix)?;
            let val = value.to_str().ok()?;
            Some((key.to_string(), val.to_string()))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_metadata_strips_prefix() {
        let mut headers = HeaderMap::new();
        headers.insert(
            format!("{}tenant", DYNAMO_METADATA_HEADER_PREFIX_DEFAULT)
                .parse::<axum::http::HeaderName>()
                .unwrap(),
            "acme".parse().unwrap(),
        );
        headers.insert(
            format!("{}user-id", DYNAMO_METADATA_HEADER_PREFIX_DEFAULT)
                .parse::<axum::http::HeaderName>()
                .unwrap(),
            "u42".parse().unwrap(),
        );
        headers.insert("x-request-id", "irrelevant".parse().unwrap());

        let meta = extract_metadata_from_headers(&headers);
        assert_eq!(meta.get("tenant").map(String::as_str), Some("acme"));
        assert_eq!(meta.get("user-id").map(String::as_str), Some("u42"));
        assert!(!meta.contains_key("x-request-id"));
    }

    #[test]
    fn test_extract_metadata_empty_on_no_prefix_match() {
        let mut headers = HeaderMap::new();
        headers.insert("content-type", "application/json".parse().unwrap());
        headers.insert("x-dynamo-request-id", "some-uuid".parse().unwrap());

        assert!(extract_metadata_from_headers(&headers).is_empty());
    }

    #[test]
    fn test_extract_metadata_ignores_wrong_prefix() {
        let mut headers = HeaderMap::new();
        // Similar prefix but not matching — must NOT appear in metadata.
        headers.insert(
            "x-not-specific-header-value",
            "should-not-appear".parse().unwrap(),
        );
        // Correct prefix — must appear.
        headers.insert(
            format!("{}keep", DYNAMO_METADATA_HEADER_PREFIX_DEFAULT)
                .parse::<axum::http::HeaderName>()
                .unwrap(),
            "yes".parse().unwrap(),
        );

        let meta = extract_metadata_from_headers(&headers);
        assert!(!meta.contains_key("x-not-specific-header-value"));
        assert!(!meta.contains_key("not-specific-header-value"));
        assert_eq!(meta.get("keep").map(String::as_str), Some("yes"));
        assert_eq!(meta.len(), 1);
    }
}
