//! What a terminal shows after a stream of its output, as text.
//!
//! A background session's lane hands back every byte sent since the connection opened, so a
//! dialog that was answered is still in it, followed by the bytes that erased it. Replaying the
//! stream onto a grid answers "what is on screen now". This is a small VT subset: text, CR, LF,
//! BS, TAB, cursor moves, erase in line and display, erase characters, save and restore, and the
//! alternate-screen switch, which starts a blank grid. Colours and every other sequence are
//! dropped. One column per character, and the grid never scrolls, so a screen taller than the
//! stream's own geometry keeps its rows rather than losing the top ones.

use std::collections::BTreeMap;

/// A stream longer than this is cut from the front. Cursor-relative paints read fine from any
/// point, and a dialog is always among the last things drawn.
pub const MAX_STREAM_CHARS: usize = 400_000;

/// No real terminal is this large. Cursor coordinates from sequence parameters are capped here,
/// so a corrupt parameter cannot make the grid allocate without bound.
const MAX_COORD: usize = 9_999;

const SCREEN_SWITCH: [usize; 3] = [1049, 1047, 47];

const ESC: char = '\x1b';

#[derive(Default)]
struct Grid {
    rows: BTreeMap<usize, Vec<char>>,
    row: usize,
    col: usize,
    saved: (usize, usize),
}

impl Grid {
    fn clear(&mut self) {
        self.rows.clear();
        self.row = 0;
        self.col = 0;
    }

    fn put(&mut self, ch: char) {
        let line = self.rows.entry(self.row).or_default();
        if line.len() < self.col {
            line.resize(self.col, ' ');
        }
        if self.col < line.len() {
            line[self.col] = ch;
        } else {
            line.push(ch);
        }
        self.col += 1;
    }

    fn erase_line(&mut self, mode: usize) {
        let col = self.col;
        let Some(line) = self.rows.get_mut(&self.row) else { return };
        match mode {
            0 => line.truncate(col),
            1 => {
                let end = (col + 1).min(line.len());
                line[..end].fill(' ');
            }
            _ => line.clear(),
        }
    }

    fn erase_display(&mut self, mode: usize) {
        let row = self.row;
        match mode {
            2 | 3 => self.rows.clear(),
            0 => {
                self.erase_line(0);
                self.rows.retain(|&r, _| r <= row);
            }
            1 => {
                self.erase_line(1);
                self.rows.retain(|&r, _| r >= row);
            }
            _ => {}
        }
    }

    fn erase_chars(&mut self, n: usize) {
        let col = self.col;
        if let Some(line) = self.rows.get_mut(&self.row) {
            let end = col.saturating_add(n).min(line.len());
            if col < end {
                line[col..end].fill(' ');
            }
        }
    }

    fn text(&self) -> String {
        let Some((&last, _)) = self.rows.last_key_value() else { return String::new() };
        let mut lines: Vec<String> = (0..=last)
            .map(|r| {
                let line: String = self.rows.get(&r).map(|l| l.iter().collect()).unwrap_or_default();
                line.trim_end().to_string()
            })
            .collect();
        while lines.last().is_some_and(String::is_empty) {
            lines.pop();
        }
        lines.join("\n")
    }

    fn csi(&mut self, params: &str, fin: char) {
        let n = numbers(params);
        let first = if n[0] == 0 { 1 } else { n[0] };
        match fin {
            'A' => self.row = self.row.saturating_sub(first),
            'B' => self.row = (self.row + first).min(MAX_COORD),
            'C' => self.col = (self.col + first).min(MAX_COORD),
            'D' => self.col = self.col.saturating_sub(first),
            'E' => (self.row, self.col) = ((self.row + first).min(MAX_COORD), 0),
            'F' => (self.row, self.col) = (self.row.saturating_sub(first), 0),
            'G' => self.col = first - 1,
            'H' | 'f' => {
                let second = n.get(1).copied().unwrap_or(0);
                self.row = first - 1;
                self.col = if second == 0 { 0 } else { second - 1 };
            }
            'd' => self.row = first - 1,
            'K' => self.erase_line(n[0]),
            'J' => self.erase_display(n[0]),
            'X' => self.erase_chars(first),
            's' => self.saved = (self.row, self.col),
            'u' => (self.row, self.col) = self.saved,
            'h' | 'l' if params.starts_with('?') && n.iter().any(|v| SCREEN_SWITCH.contains(v)) => self.clear(),
            _ => {}
        }
    }

    fn esc(&mut self, c: char) {
        match c {
            '7' => self.saved = (self.row, self.col),
            '8' => (self.row, self.col) = self.saved,
            'M' => self.row = self.row.saturating_sub(1),
            'D' => self.row += 1,
            _ => (self.row, self.col) = (self.row + 1, 0),
        }
    }

    fn control(&mut self, c: char) {
        match c {
            '\r' => self.col = 0,
            '\n' => (self.row, self.col) = (self.row + 1, 0),
            '\x08' => self.col = self.col.saturating_sub(1),
            _ => self.col = (self.col / 8 + 1) * 8,
        }
    }
}

/// A CSI's numeric parameters: a missing or non-numeric one is 0, a `:` sub-parameter is dropped.
fn numbers(params: &str) -> Vec<usize> {
    params
        .trim_start_matches(['?', '<', '>', '=', '!', ' '])
        .split(';')
        .map(|part| {
            let part = part.split(':').next().unwrap_or("");
            if !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()) {
                part.bytes().fold(0usize, |acc, b| (acc * 10 + usize::from(b - b'0')).min(MAX_COORD + 1))
            } else {
                0
            }
        })
        .collect()
}

fn is_csi_param(c: char) -> bool {
    c.is_ascii_digit() || ";:?<>=! ".contains(c)
}

fn is_dropped_control(c: char) -> bool {
    (c as u32) < 0x20 || c == '\x7f'
}

/// Applies the escape sequence that starts at `chars[i]` (an ESC) and returns the index after it.
fn escape(grid: &mut Grid, chars: &[char], i: usize) -> usize {
    let Some(&kind) = chars.get(i + 1) else { return i + 1 };
    match kind {
        '[' => {
            let start = i + 2;
            let mut j = start;
            while j < chars.len() && is_csi_param(chars[j]) {
                j += 1;
            }
            if let Some(&fin) = chars.get(j).filter(|c| ('@'..='~').contains(*c)) {
                let params: String = chars[start..j].iter().collect();
                grid.csi(&params, fin);
                return j + 1;
            }
        }
        ']' => {
            let mut j = i + 2;
            while j < chars.len() && chars[j] != '\x07' && chars[j] != ESC {
                j += 1;
            }
            match chars.get(j) {
                Some('\x07') => return j + 1,
                Some(&ESC) if chars.get(j + 1) == Some(&'\\') => return j + 2,
                _ => {}
            }
        }
        'P' => {
            if let Some(k) = (i + 2..chars.len().saturating_sub(1)).find(|&k| chars[k] == ESC && chars[k + 1] == '\\') {
                return k + 2;
            }
        }
        '(' | ')' => {
            if chars.get(i + 2).is_some_and(char::is_ascii_alphanumeric) {
                return i + 3;
            }
        }
        '7' | '8' | 'M' | 'D' | 'E' => grid.esc(kind),
        _ => {}
    }
    i + 2
}

/// The screen `stream` leaves behind, one text line per row, trailing blanks trimmed.
pub fn render(stream: &str) -> String {
    let skip = stream.chars().count().saturating_sub(MAX_STREAM_CHARS);
    let chars: Vec<char> = stream.chars().skip(skip).collect();
    let mut grid = Grid::default();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        match c {
            ESC => i = escape(&mut grid, &chars, i),
            '\r' | '\n' | '\x08' | '\t' => {
                grid.control(c);
                i += 1;
            }
            _ if is_dropped_control(c) => i += 1,
            _ => {
                grid.put(c);
                i += 1;
            }
        }
    }
    grid.text()
}

#[cfg(test)]
mod tests {
    use super::render;
    use crate::guards;

    #[test]
    fn test_text_and_line_breaks_render_as_rows() {
        assert_eq!(render("one\r\ntwo\r\n"), "one\ntwo");
    }

    #[test]
    fn test_colour_and_title_sequences_are_dropped() {
        assert_eq!(render("\x1b[1;31mred\x1b[0m\x1b]0;title\x07 ok"), "red ok");
    }

    #[test]
    fn test_erasing_lines_upward_removes_what_was_painted_there() {
        let painted = "keep\r\nold one\r\nold two";
        let erased = "\x1b[2K\x1b[1A\x1b[2K\x1b[G";
        assert_eq!(render(&format!("{painted}{erased}new")), "keep\nnew");
    }

    #[test]
    fn test_an_absolute_repaint_overwrites_and_erase_in_line_clears_the_tail() {
        let stream = "\x1b[1;1Hhello world\x1b[1;6H\x1b[K\x1b[2;3Hbelow";
        assert_eq!(render(stream), "hello\n  below");
    }

    #[test]
    fn test_erase_in_display_clears_below_the_cursor() {
        assert_eq!(render("a\r\nb\r\nc\x1b[2;1H\x1b[J"), "a");
    }

    #[test]
    fn test_switching_screens_starts_a_blank_grid() {
        assert_eq!(render("old\x1b[?1049hnew"), "new");
    }

    #[test]
    fn test_saved_cursor_comes_back() {
        assert_eq!(render("ab\x1b7\r\nxy\x1b8Z"), "abZ\nxy");
    }

    #[test]
    fn test_a_screen_with_no_escapes_is_text() {
        assert_eq!(guards::current_screen("plain\ntext"), "plain\ntext");
    }
}
