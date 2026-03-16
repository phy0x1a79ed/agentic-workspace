---
name: reporting
type: sop
tags: [report, publishing, visualization, methods]
---

# Reporting

## Report Package Structure

Each report is a self-contained directory under `reports/{project}/{report_name}/`:

```
reports/<project>/<report_name>/
  methods.md              # Methods narrative (source)
  methods.pdf             # Rendered methods document
  tables/
    summary_stats.csv     # Data tables in CSV format
    differential.csv
  figures/
    figure1.svg           # Vector format (primary)
    figure1.png           # Raster format (fallback/preview)
    figure2.svg
    figure2.png
```

### Component Requirements

| Component      | Format     | Notes                                      |
|----------------|------------|--------------------------------------------|
| Methods        | `.md` + `.pdf` | Markdown source, rendered PDF for distribution |
| Data tables    | `.csv`     | UTF-8 encoded, header row required         |
| Visualizations | `.svg` + `.png` | SVG as primary, PNG as fallback (300 DPI minimum) |

## Creating a Report Package

```bash
# Create directory structure
mkdir -p reports/<project>/<report_name>/{tables,figures}

# Initialize methods document
cat > reports/<project>/<report_name>/methods.md << 'EOF'
---
title: "<Report Title>"
date: YYYY-MM-DD
project: <project>
---

# Methods

## Data Sources

## Processing

## Analysis

## Statistical Tests
EOF
```

## Rendering Methods to PDF

```bash
pandoc reports/<project>/<report_name>/methods.md \
  -o reports/<project>/<report_name>/methods.pdf \
  --pdf-engine=xelatex
```

## Publishing Checklist

- [ ] `methods.md` is complete with all sections filled
- [ ] `methods.pdf` is rendered and matches the current `.md` source
- [ ] All CSV files have headers and no missing column names
- [ ] All figures exist in both SVG and PNG formats
- [ ] Figure file names are descriptive (not `plot1.svg`)
- [ ] No absolute file paths appear in methods or figure metadata
- [ ] Report directory contains no temporary or intermediate files
- [ ] All data tables referenced in methods are present in `tables/`
- [ ] All figures referenced in methods are present in `figures/`
