Gemini said
In this conversation, we explored an advanced agentic workflow for 2026, focusing on automating the creation of interactive, animated presentations using Claude Code and Plotly.

Here are the key technical pillars we established:

1. Skill-Based Personalization
Instead of manually coaching the AI on your style every time, we utilized Claude Skills (.claude/skills/). By layering a custom "Aesthetic Skill" over the official Plotly skill, you can force the agent to follow specific brand guidelines (colors, fonts, and animation speeds) without modifying the core library.

2. Code-Based Template Injection
We integrated a sophisticated Python wrapper (BaseFigure and ApplyTemplate) that solves common Plotly headaches, such as:

Automatic Subplot Tracking: Ensuring axis indices don't break when using "problematic" traces like Pie charts.

Functional Styling: Passing your boilerplate logic as a "Library Tool" so the agent calls your functions rather than hallucinating its own.

3. The "No-Overseer" Parallel Architecture
To build multiple presentations (e.g., Q1 Sales vs. Investor Deck) simultaneously without conflicts, we used a Scoped Workspace strategy:

Git Worktrees: Each agent works in a physically isolated folder that shares a common Git history.

Symlink Injection: Shared assets (logos, images) and the core library (lib/) are symlinked into these worktrees as read-only volumes.

Isolated Builds: Each deck maintains its own build.sh and local .claude/skills/ for deck-specific logic (e.g., a specific "Investor" color palette).

4. Implementation Readiness
We concluded with a setup_workspace.sh script that automates the creation of this environment, initializing the directory structure, the CLAUDE.md orchestration rules, and the necessary Git configuration to support parallel agentic sessions.

This setup moves beyond "vibe-coding" and creates a production-grade pipeline where agents act as specialized, autonomous deck-builders sharing a common brain for visual standards.

Building a Plotly AI Agent

This video provides a foundational look at how to construct a Plotly-focused AI agent, which is the core building block for the automated visualization engine we discussed.