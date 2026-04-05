---
name: plotly
type: reference
scope: workspace
tags: [visualization, plotly, figures, charts, export, kaleido]
requires: []
description: BaseFigure/ApplyTemplate, palettes, Kaleido export
---

# Plotly Quick Reference

## BaseFigure / ApplyTemplate Pattern

Use a base figure factory to enforce consistent styling across all plots:

```python
from plotly import graph_objects as go

def base_figure(**kwargs) -> go.Figure:
    """Return a Figure with project-standard template applied."""
    fig = go.Figure(**kwargs)
    fig.update_layout(
        template="plotly_white",
        font=dict(family="Arial", size=12),
        margin=dict(l=60, r=30, t=50, b=50),
    )
    return fig
```

Apply a shared template object for multi-plot consistency:

```python
import plotly.io as pio

pio.templates["project"] = go.layout.Template(
    layout=dict(
        colorway=COLOR_PALETTE,
        font_family="Arial",
    )
)
pio.templates.default = "plotly_white+project"
```

## Color Palettes

```python
# Categorical (up to 10 groups)
COLOR_PALETTE = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]

# Sequential: px.colors.sequential.Viridis, .Blues, .Reds
# Diverging: px.colors.diverging.RdBu, .Spectral
```

## Kaleido PNG/SVG Export

```python
fig.write_image("plot.png", width=800, height=500, scale=2, engine="kaleido")
fig.write_image("plot.svg", engine="kaleido")
```

**WSL2 note:** If Kaleido hangs or segfaults, set the environment variable before import:

```bash
export KALEIDO_DISABLE_GPU=1
```

Or in Python:

```python
import os
os.environ["KALEIDO_DISABLE_GPU"] = "1"
```

Alternatively pass `--disable-gpu` if invoking Kaleido directly.

## Subplots and Axis Index Tracking

Use `make_subplots` and track axis indices through BaseFigure to avoid off-by-one errors:

```python
from plotly.subplots import make_subplots

fig = make_subplots(rows=2, cols=2, subplot_titles=("A", "B", "C", "D"))

# Axes are named: xaxis/yaxis (1,1), xaxis2/yaxis2 (1,2), xaxis3/yaxis3 (2,1), ...
# When adding traces, specify row= and col= rather than manually setting xaxis/yaxis:
fig.add_trace(go.Scatter(x=x, y=y), row=1, col=2)
```

Key gotchas:
- `row` and `col` are 1-indexed.
- `fig.update_xaxes(...)` accepts `row`/`col` to target a specific subplot.
- Shared axes (`shared_xaxes=True`) suppress tick labels on inner plots; use `fig.update_xaxes(showticklabels=True, row=..., col=...)` to override.
