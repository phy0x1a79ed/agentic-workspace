//! Friend-side TUI for the probe binary.
//!
//! Design direction: *letterpress dispatch* — three content streams
//! (exec, chat, system) get distinct typographic roles so the eye can
//! locate them without leaning on color alone. Palette is the roma-ui
//! design used by the web-ui frontend
//! (`projects/awm/web-ui/frontend/src/app.css`).

use crossterm::event::{poll, read, Event, KeyCode, KeyEventKind, KeyModifiers};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Paragraph, Wrap},
    Terminal,
};
use std::collections::{HashMap, VecDeque};
use std::io;
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

const MAX_BLOCKS: usize = 200;
const RIGHT_GUTTER: u16 = 2;

// roma-ui palette. Source of truth: web-ui/frontend/src/app.css
const FG: Color = Color::Rgb(232, 232, 236);
const TEXT2: Color = Color::Rgb(152, 152, 168);
const TEXT3: Color = Color::Rgb(82, 82, 94);
const ACCENT: Color = Color::Rgb(59, 130, 246);
const WARN: Color = Color::Rgb(234, 179, 8);
const DANGER: Color = Color::Rgb(239, 68, 68);
const OK: Color = Color::Rgb(16, 185, 129);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutKind {
    Stdout,
    Stderr,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Who {
    Operator,
    Friend,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    Linking,
    Live,
    Ended,
}

#[derive(Debug, Clone)]
pub enum TuiMsg {
    System(String),
    Error(String),
    ExecStarted { id: u64, cmd: String },
    ExecOutput { id: u64, kind: OutKind, text: String },
    ExecExit { id: u64, code: Option<i32> },
    Chat { from: Who, message: String },
    Scope(Option<String>),
    Status(Status),
}

#[derive(Debug)]
pub enum UiEvent {
    SendChat(String),
    Disconnect,
}

#[derive(Debug, Clone)]
pub struct HeaderInit {
    pub name: String,
    pub transport: &'static str,
}

#[derive(Debug, Clone)]
struct ExitInfo {
    code: Option<i32>,
    elapsed: Duration,
}

#[derive(Debug, Clone)]
struct OutLine {
    kind: OutKind,
    text: String,
}

#[derive(Debug, Clone)]
enum Block {
    Exec {
        id: u64,
        cmd: String,
        started: Instant,
        output: Vec<OutLine>,
        exit: Option<ExitInfo>,
    },
    Chat {
        from: Who,
        message: String,
        at: Instant,
    },
    System {
        message: String,
    },
    Error {
        message: String,
    },
}

impl Block {
    fn is_system_ish(&self) -> bool {
        matches!(self, Block::System { .. } | Block::Error { .. })
    }
}

struct TuiState {
    name: String,
    scope: Option<String>,
    transport: &'static str,
    status: Status,
    session_start: Instant,
    blocks: VecDeque<Block>,
    exec_index: HashMap<u64, usize>,
    input: String,
}

impl TuiState {
    fn new(header: HeaderInit) -> Self {
        Self {
            name: header.name,
            scope: None,
            transport: header.transport,
            status: Status::Linking,
            session_start: Instant::now(),
            blocks: VecDeque::new(),
            exec_index: HashMap::new(),
            input: String::new(),
        }
    }

    fn push(&mut self, msg: TuiMsg) {
        match msg {
            TuiMsg::System(message) => self.add_block(Block::System { message }),
            TuiMsg::Error(message) => self.add_block(Block::Error { message }),
            TuiMsg::ExecStarted { id, cmd } => {
                let block = Block::Exec {
                    id,
                    cmd,
                    started: Instant::now(),
                    output: Vec::new(),
                    exit: None,
                };
                self.add_block(block);
                // Recompute the index — add_block may have evicted from the front.
                self.rebuild_exec_index();
            }
            TuiMsg::ExecOutput { id, kind, text } => {
                let lines: Vec<String> = text
                    .split('\n')
                    .map(|s| s.trim_end_matches('\r').to_string())
                    .collect();
                // Drop a trailing empty produced by a closing '\n'.
                let lines: Vec<String> = if lines.last().map(|s| s.is_empty()).unwrap_or(false)
                    && lines.len() > 1
                {
                    lines[..lines.len() - 1].to_vec()
                } else {
                    lines
                };
                if let Some(&idx) = self.exec_index.get(&id) {
                    if let Some(Block::Exec { output, .. }) = self.blocks.get_mut(idx) {
                        for line in lines {
                            if line.is_empty() {
                                continue;
                            }
                            output.push(OutLine { kind, text: line });
                        }
                    }
                }
            }
            TuiMsg::ExecExit { id, code } => {
                if let Some(&idx) = self.exec_index.get(&id) {
                    if let Some(Block::Exec { started, exit, .. }) = self.blocks.get_mut(idx) {
                        *exit = Some(ExitInfo {
                            code,
                            elapsed: started.elapsed(),
                        });
                    }
                }
            }
            TuiMsg::Chat { from, message } => self.add_block(Block::Chat {
                from,
                message,
                at: Instant::now(),
            }),
            TuiMsg::Scope(scope) => {
                self.scope = scope;
            }
            TuiMsg::Status(s) => {
                self.status = s;
            }
        }
    }

    fn add_block(&mut self, block: Block) {
        if self.blocks.len() >= MAX_BLOCKS {
            // Drop the oldest block; rebuild exec_index after.
            self.blocks.pop_front();
            self.rebuild_exec_index();
        }
        if let Block::Exec { id, .. } = &block {
            self.exec_index.insert(*id, self.blocks.len());
        }
        self.blocks.push_back(block);
    }

    fn rebuild_exec_index(&mut self) {
        self.exec_index.clear();
        for (idx, block) in self.blocks.iter().enumerate() {
            if let Block::Exec { id, .. } = block {
                self.exec_index.insert(*id, idx);
            }
        }
    }
}

fn fmt_t_plus(d: Duration) -> String {
    let secs = d.as_secs();
    format!("t+{:02}:{:02}", secs / 60, secs % 60)
}

fn fmt_elapsed_ms(d: Duration) -> String {
    let ms = d.as_millis();
    if ms < 10_000 {
        format!("{} ms", ms)
    } else {
        format!("{:.1} s", d.as_secs_f32())
    }
}

/// Build a row with `left` left-aligned and `right` right-aligned in `width`
/// columns. Right text rendered in `right_style`; if there's not enough
/// room, the right text is omitted.
fn justified(left_spans: Vec<Span<'static>>, right: String, right_style: Style, width: u16) -> Line<'static> {
    let left_w: usize = left_spans.iter().map(|s| s.content.chars().count()).sum();
    let right_w = right.chars().count();
    let usable = width.saturating_sub(RIGHT_GUTTER) as usize;
    if left_w + right_w + 1 > usable {
        return Line::from(left_spans);
    }
    let pad = usable - left_w - right_w;
    let mut spans = left_spans;
    spans.push(Span::raw(" ".repeat(pad)));
    spans.push(Span::styled(right, right_style));
    Line::from(spans)
}

fn render_header(state: &TuiState, width: u16) -> Line<'static> {
    let scope = state.scope.clone().unwrap_or_else(|| "—".to_string());
    let left = vec![
        Span::raw("  "),
        Span::styled(
            state.name.clone(),
            Style::new().fg(FG).add_modifier(Modifier::BOLD),
        ),
        Span::styled("  →  ", Style::new().fg(TEXT3)),
        Span::styled(scope, Style::new().fg(TEXT2)),
    ];
    let status_text = match state.status {
        Status::Linking => "linking",
        Status::Live => "live",
        Status::Ended => "ended",
    };
    let status_color = match state.status {
        Status::Linking => WARN,
        Status::Live => OK,
        Status::Ended => TEXT3,
    };
    let right = format!("{}  ·  {}", state.transport, status_text);
    // The "·" portion gets text3 styling via post-hoc trick: we color the
    // whole right text with TEXT3 first, then overlay a coloured status
    // by building it ourselves.
    let right_w = right.chars().count();
    let left_w: usize = left.iter().map(|s| s.content.chars().count()).sum();
    let usable = width.saturating_sub(RIGHT_GUTTER) as usize;
    let mut spans = left;
    if left_w + right_w + 1 <= usable {
        let pad = usable - left_w - right_w;
        spans.push(Span::raw(" ".repeat(pad)));
        spans.push(Span::styled(state.transport.to_string(), Style::new().fg(TEXT3)));
        spans.push(Span::styled("  ·  ", Style::new().fg(TEXT3)));
        spans.push(Span::styled(
            status_text.to_string(),
            Style::new().fg(status_color),
        ));
    }
    Line::from(spans)
}

fn render_exec_block(b: &Block, session_start: Instant, width: u16) -> Vec<Line<'static>> {
    let (id, cmd, started, output, exit) = match b {
        Block::Exec {
            id,
            cmd,
            started,
            output,
            exit,
        } => (*id, cmd, *started, output, exit),
        _ => unreachable!(),
    };
    let _ = id;
    let t_plus = fmt_t_plus(started.duration_since(session_start));
    let mut lines = Vec::new();
    // ▎ cmd                       t+MM:SS
    let header_left = vec![
        Span::raw("  "),
        Span::styled(
            "▎ ",
            Style::new().fg(WARN).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            cmd.clone(),
            Style::new().fg(FG).add_modifier(Modifier::BOLD),
        ),
    ];
    lines.push(justified(header_left, t_plus, Style::new().fg(TEXT3), width));
    // hung-indented output (4 cols)
    for line in output {
        let style = match line.kind {
            OutKind::Stdout => Style::new().fg(FG),
            OutKind::Stderr => Style::new().fg(DANGER),
        };
        lines.push(Line::from(vec![
            Span::raw("    "),
            Span::styled(line.text.clone(), style),
        ]));
    }
    // ─ ok / fail N                    NN ms
    if let Some(exit) = exit {
        let (verdict, verdict_color) = match exit.code {
            Some(0) => ("ok".to_string(), OK),
            Some(c) => (format!("fail {}", c), DANGER),
            None => ("fail".to_string(), DANGER),
        };
        let exit_left = vec![
            Span::raw("    "),
            Span::styled("─ ", Style::new().fg(TEXT3)),
            Span::styled(verdict, Style::new().fg(verdict_color)),
        ];
        lines.push(justified(
            exit_left,
            fmt_elapsed_ms(exit.elapsed),
            Style::new().fg(TEXT3),
            width,
        ));
    }
    lines
}

fn render_chat_block(b: &Block, session_start: Instant, width: u16) -> Vec<Line<'static>> {
    let (from, message, at) = match b {
        Block::Chat { from, message, at } => (*from, message.clone(), *at),
        _ => unreachable!(),
    };
    let (label, color) = match from {
        Who::Operator => ("operator", ACCENT),
        Who::Friend => ("you", OK),
    };
    let t_plus = fmt_t_plus(at.duration_since(session_start));
    let header_left = vec![
        Span::raw("  "),
        Span::styled("│ ", Style::new().fg(color)),
        Span::styled(
            label.to_string(),
            Style::new().fg(color).add_modifier(Modifier::BOLD),
        ),
    ];
    let mut lines = Vec::new();
    lines.push(justified(header_left, t_plus, Style::new().fg(TEXT3), width));
    // body, hung-indented by 4 cols, split on internal newlines
    for line in message.split('\n') {
        let line = line.trim_end_matches('\r');
        lines.push(Line::from(vec![
            Span::raw("    "),
            Span::styled(line.to_string(), Style::new().fg(FG)),
        ]));
    }
    lines
}

fn render_system_block(b: &Block) -> Vec<Line<'static>> {
    let (message, is_error) = match b {
        Block::System { message, .. } => (message.clone(), false),
        Block::Error { message, .. } => (message.clone(), true),
        _ => unreachable!(),
    };
    let color = if is_error { DANGER } else { TEXT3 };
    let mut lines = Vec::new();
    for line in message.split('\n') {
        let line = line.trim_end_matches('\r');
        lines.push(Line::from(vec![
            Span::raw("  "),
            Span::styled(line.to_string(), Style::new().fg(color)),
        ]));
    }
    lines
}

fn render_feed(state: &TuiState, width: u16, height: u16) -> Text<'static> {
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut prev_was_system: Option<bool> = None;
    for block in state.blocks.iter() {
        let block_is_sys = block.is_system_ish();
        // Blank row above each block, except: between two consecutive system blocks,
        // or before the very first block.
        if let Some(prev_sys) = prev_was_system {
            if !(prev_sys && block_is_sys) {
                lines.push(Line::from(""));
            }
        }
        prev_was_system = Some(block_is_sys);
        let block_lines = match block {
            Block::Exec { .. } => render_exec_block(block, state.session_start, width),
            Block::Chat { .. } => render_chat_block(block, state.session_start, width),
            Block::System { .. } | Block::Error { .. } => render_system_block(block),
        };
        lines.extend(block_lines);
    }
    // Keep the last `height` rows so newest content stays visible.
    if lines.len() > height as usize {
        let drop = lines.len() - height as usize;
        lines = lines.split_off(drop);
    }
    Text::from(lines)
}

fn render_composer(width: u16, input: &str) -> Vec<Line<'static>> {
    // Bracketed thin rule (╶─…─╴) in muted text3.
    let inner = width.saturating_sub(4) as usize;
    let rule = if inner >= 2 {
        let mut s = String::from("╶");
        s.push_str(&"─".repeat(inner.saturating_sub(2)));
        s.push('╴');
        s
    } else {
        "─".repeat(inner)
    };
    let rule_line = Line::from(vec![
        Span::raw("  "),
        Span::styled(rule, Style::new().fg(TEXT3)),
    ]);
    let prompt_line = Line::from(vec![
        Span::raw("   "),
        Span::styled("▸ ", Style::new().fg(ACCENT)),
        Span::styled(input.to_string(), Style::new().fg(FG)),
        Span::styled("_", Style::new().fg(TEXT3)),
    ]);
    vec![rule_line, prompt_line]
}

fn render_footer() -> Line<'static> {
    Line::from(vec![
        Span::raw("  "),
        Span::styled(
            "esc disconnect  ·  enter send  ·  ctrl-c quit",
            Style::new().fg(TEXT3),
        ),
    ])
}

fn draw(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    state: &TuiState,
) -> Result<(), Box<dyn std::error::Error>> {
    terminal.draw(|f| {
        let area = f.area();
        let chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(1), // header row
                Constraint::Length(1), // header underline
                Constraint::Min(1),    // feed
                Constraint::Length(2), // composer (rule + input)
                Constraint::Length(1), // footer
            ])
            .split(area);

        let header_area: Rect = chunks[0];
        let underline_area: Rect = chunks[1];
        let feed_area: Rect = chunks[2];
        let composer_area: Rect = chunks[3];
        let footer_area: Rect = chunks[4];

        let header = render_header(state, header_area.width);
        f.render_widget(Paragraph::new(header), header_area);

        // Subtle full-width rule under header
        let underline = "─".repeat(underline_area.width.saturating_sub(4) as usize);
        let underline_line = Line::from(vec![
            Span::raw("  "),
            Span::styled(underline, Style::new().fg(TEXT3)),
        ]);
        f.render_widget(Paragraph::new(underline_line), underline_area);

        let feed_text = render_feed(state, feed_area.width, feed_area.height);
        f.render_widget(
            Paragraph::new(feed_text).wrap(Wrap { trim: false }),
            feed_area,
        );

        let composer_lines = render_composer(composer_area.width, &state.input);
        f.render_widget(
            Paragraph::new(Text::from(composer_lines)),
            composer_area,
        );

        f.render_widget(Paragraph::new(render_footer()), footer_area);
    })?;
    Ok(())
}

struct TerminalGuard;

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = crossterm::terminal::disable_raw_mode();
        let _ = crossterm::execute!(io::stdout(), crossterm::terminal::LeaveAlternateScreen);
    }
}

fn setup_terminal() -> Result<TerminalGuard, Box<dyn std::error::Error>> {
    crossterm::terminal::enable_raw_mode()?;
    let mut stdout = io::stdout();
    crossterm::execute!(stdout, crossterm::terminal::EnterAlternateScreen)?;
    Ok(TerminalGuard)
}

pub async fn run_tui(
    header_init: HeaderInit,
    mut msg_rx: mpsc::Receiver<TuiMsg>,
    ui_tx: mpsc::Sender<UiEvent>,
) {
    let _guard = match setup_terminal() {
        Ok(g) => g,
        Err(e) => {
            eprintln!("TUI setup failed: {e}");
            return;
        }
    };

    let mut terminal = match Terminal::new(CrosstermBackend::new(io::stdout())) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("TUI terminal init failed: {e}");
            return;
        }
    };
    let _ = terminal.hide_cursor();

    let mut state = TuiState::new(header_init);
    let mut shutdown = false;
    let mut tick = tokio::time::interval(Duration::from_millis(50));

    while !shutdown {
        tokio::select! {
            maybe_msg = msg_rx.recv() => {
                match maybe_msg {
                    Some(msg) => state.push(msg),
                    None => shutdown = true,
                }
            }
            _ = tick.tick() => {
                while poll(Duration::ZERO).unwrap_or(false) {
                    match read() {
                        Ok(Event::Key(key)) if key.kind == KeyEventKind::Press => {
                            match key.code {
                                KeyCode::Char(c) if key.modifiers.contains(KeyModifiers::CONTROL) => {
                                    if matches!(c, 'd' | 'c') {
                                        let _ = ui_tx.send(UiEvent::Disconnect).await;
                                        shutdown = true;
                                    }
                                }
                                KeyCode::Esc => {
                                    let _ = ui_tx.send(UiEvent::Disconnect).await;
                                    shutdown = true;
                                }
                                KeyCode::Enter => {
                                    let msg = std::mem::take(&mut state.input);
                                    if !msg.is_empty() {
                                        let _ = ui_tx.send(UiEvent::SendChat(msg)).await;
                                    }
                                }
                                KeyCode::Backspace => {
                                    state.input.pop();
                                }
                                KeyCode::Char(c) => {
                                    state.input.push(c);
                                }
                                _ => {}
                            }
                        }
                        Ok(Event::Resize(_, _)) => {}
                        _ => {}
                    }
                }
            }
        }

        if shutdown {
            break;
        }

        if let Err(e) = draw(&mut terminal, &state) {
            eprintln!("TUI render error: {e}");
            break;
        }
    }

    let _ = terminal.show_cursor();
    let _ = ui_tx.send(UiEvent::Disconnect).await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fmt_t_plus_basic() {
        assert_eq!(fmt_t_plus(Duration::from_secs(0)), "t+00:00");
        assert_eq!(fmt_t_plus(Duration::from_secs(7)), "t+00:07");
        assert_eq!(fmt_t_plus(Duration::from_secs(75)), "t+01:15");
    }

    #[test]
    fn fmt_elapsed_ms_basic() {
        assert_eq!(fmt_elapsed_ms(Duration::from_millis(62)), "62 ms");
        assert_eq!(fmt_elapsed_ms(Duration::from_millis(9999)), "9999 ms");
        assert_eq!(fmt_elapsed_ms(Duration::from_millis(12500)), "12.5 s");
    }

    fn make_state() -> TuiState {
        TuiState::new(HeaderInit {
            name: "tuitest".into(),
            transport: "WebRTC",
        })
    }

    #[test]
    fn exec_output_splits_on_newline() {
        let mut s = make_state();
        s.push(TuiMsg::ExecStarted {
            id: 1,
            cmd: "loop".into(),
        });
        s.push(TuiMsg::ExecOutput {
            id: 1,
            kind: OutKind::Stdout,
            text: "line 1\nline 2\nline 3\n".into(),
        });
        let Some(Block::Exec { output, .. }) = s.blocks.back() else {
            panic!("expected exec block");
        };
        let texts: Vec<&str> = output.iter().map(|l| l.text.as_str()).collect();
        assert_eq!(texts, vec!["line 1", "line 2", "line 3"]);
    }

    #[test]
    fn exec_output_drops_trailing_empty_only() {
        let mut s = make_state();
        s.push(TuiMsg::ExecStarted {
            id: 1,
            cmd: "x".into(),
        });
        s.push(TuiMsg::ExecOutput {
            id: 1,
            kind: OutKind::Stdout,
            text: "a\n\nb\n".into(),
        });
        let Some(Block::Exec { output, .. }) = s.blocks.back() else {
            panic!("expected exec block");
        };
        // Internal blank line is dropped (we skip empty segments); trailing
        // blank also dropped.
        let texts: Vec<&str> = output.iter().map(|l| l.text.as_str()).collect();
        assert_eq!(texts, vec!["a", "b"]);
    }

    #[test]
    fn exec_exit_records_code_and_elapsed() {
        let mut s = make_state();
        s.push(TuiMsg::ExecStarted {
            id: 1,
            cmd: "x".into(),
        });
        std::thread::sleep(Duration::from_millis(5));
        s.push(TuiMsg::ExecExit {
            id: 1,
            code: Some(2),
        });
        let Some(Block::Exec { exit, .. }) = s.blocks.back() else {
            panic!("expected exec block");
        };
        let exit = exit.as_ref().unwrap();
        assert_eq!(exit.code, Some(2));
        assert!(exit.elapsed.as_millis() >= 5);
    }

    #[test]
    fn scope_and_status_update() {
        let mut s = make_state();
        assert_eq!(s.status, Status::Linking);
        assert!(s.scope.is_none());
        s.push(TuiMsg::Scope(Some("demo-scope".into())));
        s.push(TuiMsg::Status(Status::Live));
        assert_eq!(s.scope.as_deref(), Some("demo-scope"));
        assert_eq!(s.status, Status::Live);
    }
}
