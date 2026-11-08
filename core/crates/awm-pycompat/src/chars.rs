//! Python's per-character predicates and lowercase mapping.

use crate::tables;
use std::cmp::Ordering;

fn in_ranges(table: &[(u32, u32)], c: char) -> bool {
    let c = c as u32;
    table
        .binary_search_by(|&(lo, hi)| {
            if hi < c {
                Ordering::Less
            } else if lo > c {
                Ordering::Greater
            } else {
                Ordering::Equal
            }
        })
        .is_ok()
}

/// `re`'s `\w` on a str pattern: `isalnum()` or `_`. Combining marks are not word characters.
pub fn is_word(c: char) -> bool {
    in_ranges(tables::WORD, c)
}

/// `str.isspace()`, which includes `\x1c`-`\x1f`. Also `re`'s `\s`.
pub fn is_space(c: char) -> bool {
    in_ranges(tables::SPACE, c)
}

/// `str.isdigit()`.
pub fn is_digit(c: char) -> bool {
    in_ranges(tables::DIGIT, c)
}

/// `str.isdecimal()`. Also `re`'s `\d`.
pub fn is_decimal(c: char) -> bool {
    in_ranges(tables::DECIMAL, c)
}

/// `str.isprintable()`, which decides what `repr` escapes.
pub fn is_printable(c: char) -> bool {
    in_ranges(tables::PRINTABLE, c)
}

/// `str.splitlines()`'s line boundaries. `\x1f` is whitespace but not a line boundary.
pub fn is_linebreak(c: char) -> bool {
    matches!(
        c,
        '\n' | '\x0b' | '\x0c' | '\r' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}'
    )
}

pub(crate) fn sigma_cased(c: char) -> bool {
    in_ranges(tables::SIGMA_CASED, c)
}

pub(crate) fn sigma_skip(c: char) -> bool {
    in_ranges(tables::SIGMA_SKIP, c)
}

/// The full lowercase of one character, outside the final-sigma context.
pub fn lower_char(c: char, out: &mut String) {
    match tables::LOWER.binary_search_by_key(&(c as u32), |&(cp, _)| cp) {
        Ok(i) => out.push_str(tables::LOWER[i].1),
        Err(_) => out.push(c),
    }
}
