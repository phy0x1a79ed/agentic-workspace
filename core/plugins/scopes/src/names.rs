//! Names that become filesystem paths. A validated name, joined onto the workspace layout,
//! cannot escape it: no traversal, no absolute path, no hidden entry, no NUL.
//!
//! Scope names may nest (`fabfos/dev`), each segment checked as a flat name. Project names
//! never nest: a slashed project would put a second bare repository one level down.

use awm_pycompat::repr::str_repr;

use crate::Fail;

const FORBIDDEN: [char; 3] = ['/', '\\', '\0'];
const FORBIDDEN_IN_SEGMENT: [char; 2] = ['\\', '\0'];

fn segment(seg: &str, kind: &str, whole: &str) -> Result<(), Fail> {
    let shown = str_repr(whole);
    if seg.is_empty() {
        return Err(Fail::value(format!("{kind} cannot contain an empty segment (got {shown})")));
    }
    if seg == "." || seg == ".." {
        return Err(Fail::value(format!("{kind} cannot contain '.' or '..' (got {shown})")));
    }
    if seg.starts_with('.') {
        return Err(Fail::value(format!("{kind} cannot have a segment starting with '.' (got {shown})")));
    }
    for ch in FORBIDDEN_IN_SEGMENT {
        if seg.contains(ch) {
            return Err(Fail::value(format!("{kind} cannot contain {} (got {shown})", str_repr(&ch.to_string()))));
        }
    }
    Ok(())
}

/// A flat name, as a project name always is.
pub fn validate_name(name: &str, kind: &str) -> Result<(), Fail> {
    if name.is_empty() {
        return Err(Fail::value(format!("{kind} must be a non-empty string")));
    }
    if name == "." || name == ".." {
        return Err(Fail::value(format!("{kind} cannot be '.' or '..'")));
    }
    if name.starts_with('.') {
        return Err(Fail::value(format!("{kind} cannot start with '.' (got {})", str_repr(name))));
    }
    for ch in FORBIDDEN {
        if name.contains(ch) {
            return Err(Fail::value(format!("{kind} cannot contain {} (got {})", str_repr(&ch.to_string()), str_repr(name))));
        }
    }
    Ok(())
}

pub fn validate_project(name: &str) -> Result<(), Fail> {
    validate_name(name, "project name")
}

/// A scope name, which may nest with `/`.
pub fn validate_scope(name: &str) -> Result<(), Fail> {
    let kind = "scope name";
    if name.is_empty() {
        return Err(Fail::value(format!("{kind} must be a non-empty string")));
    }
    if name.starts_with('/') || name.ends_with('/') {
        return Err(Fail::value(format!("{kind} cannot start or end with '/' (got {})", str_repr(name))));
    }
    for seg in name.split('/') {
        segment(seg, kind, name)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flat_names() {
        for bad in ["", ".", "..", ".hidden", "with/slash", "with\\backslash", "with\0null"] {
            assert!(validate_name(bad, "name").is_err(), "{bad:?}");
        }
        for good in ["foo", "foo-bar", "foo_bar", "foo.bar", "Foo123"] {
            assert!(validate_name(good, "name").is_ok(), "{good:?}");
        }
        assert_eq!(validate_name("a/b", "project name").unwrap_err().message, "project name cannot contain '/' (got 'a/b')");
    }

    #[test]
    fn nested_names() {
        for bad in ["", ".", "..", ".hidden", "with\\backslash", "with\0null", "/abs", "trail/", "a//b", "a/../b", "a/.x", "../escape"] {
            assert!(validate_scope(bad).is_err(), "{bad:?}");
        }
        for good in ["dev", "fabfos/dev", "a/b/c", "metasmith/dev"] {
            assert!(validate_scope(good).is_ok(), "{good:?}");
        }
        assert_eq!(validate_scope("a/../b").unwrap_err().message, "scope name cannot contain '.' or '..' (got 'a/../b')");
    }
}
