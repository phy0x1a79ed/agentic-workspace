---
name: Metasmith
type: tool
tags: [pipeline, metasmith, bioinformatics]
---

# Metasmith Quick Reference

Metasmith is a custom Python pipeline tool for metadata processing. It uses composable transform steps to clean, validate, and reshape metadata tables.

## Environment Setup

```bash
mamba activate msm_env
```

Ensure `msm_env` includes: `metasmith`, `pandas`, `pyyaml`, and any project-specific dependencies.

## Pipeline Structure

A pipeline is a sequence of **transforms** applied to a DataFrame:

```python
from metasmith import Pipeline

pipeline = Pipeline([
    ("rename_cols", RenameColumns(mapping={"old": "new"})),
    ("cast_types", CastTypes(schema={"age": "int64", "date": "datetime64[ns]"})),
    ("validate", ValidateNotNull(columns=["sample_id", "date"])),
    ("derive", DeriveColumn(name="year", expr=lambda df: df["date"].dt.year)),
])

result = pipeline.run(input_df)
```

Each transform is a composable step: it receives a DataFrame and returns a DataFrame. Steps can be reordered, removed, or inserted without changing the others.

## Configuration

Pipelines are typically driven by a YAML config:

```yaml
pipeline:
  - rename_cols:
      mapping:
        old_name: new_name
  - cast_types:
      schema:
        age: int64
        collection_date: "datetime64[ns]"
  - validate:
      not_null: [sample_id, collection_date]
```

Load and run:

```python
from metasmith import Pipeline

pipeline = Pipeline.from_yaml("pipeline_config.yml")
result = pipeline.run(input_df)
```

## Common Gotchas

- **Dtype mismatches:** Casting a column with unexpected values (e.g., `"N/A"` in a numeric column) raises silently or produces `NaN`. Clean or coerce before casting. Use `errors="coerce"` in CastTypes if partial failures are acceptable.
- **Missing columns:** A transform referencing a column that does not exist in the input will raise a `KeyError`. Guard with a column-check step or make transforms conditional.
- **Order dependence:** Renaming must happen before any step that references the new column name. Validate must happen after casting so types are correct.
- **Large files:** For datasets that exceed memory, process in chunks or use the `chunksize` parameter if supported by the data loader.
