use sha2::{Digest, Sha256};

use crate::model::SourceSpan;

pub fn semantic_id(
    namespace: &str,
    project_path: &str,
    pou: &str,
    span: Option<&SourceSpan>,
    kind: &str,
    ordinal: usize,
) -> String {
    let span_key = span
        .map(|s| {
            format!(
                "{}:{}:{}:{}:{}",
                s.file, s.start_offset, s.end_offset, s.start_line, s.start_column
            )
        })
        .unwrap_or_else(|| "synthetic".to_string());
    let key = format!("{namespace}|{project_path}|{pou}|{span_key}|{kind}|{ordinal}");
    let digest = Sha256::digest(key.as_bytes());
    format!("{namespace}:{}", hex::encode(&digest[..16]))
}

pub fn file_component(value: &str) -> String {
    value
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '-' | '_') {
                c
            } else {
                '_'
            }
        })
        .collect()
}
