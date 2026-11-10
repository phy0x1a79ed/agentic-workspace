//! Throwaway: print each Lance table or parquet file's schema, row count and first rows.
use futures::TryStreamExt;
use lancedb::query::{ExecutableQuery, QueryBase};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    for path in std::env::args().skip(1) {
        if path.ends_with(".parquet") {
            let f = std::fs::File::open(&path)?;
            let b = parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder::try_new(f)?;
            println!("== {path} rows={}\n{:#?}", b.metadata().file_metadata().num_rows(), b.schema().fields().iter().map(|f| format!("{}: {}", f.name(), f.data_type())).collect::<Vec<_>>());
            let mut r = b.with_batch_size(2).build()?;
            if let Some(batch) = r.next() { show(&batch?); }
            continue;
        }
        let db = lancedb::connect(&path).execute().await?;
        for name in db.table_names().execute().await? {
            let t = db.open_table(&name).execute().await?;
            let s = t.schema().await?;
            println!("== {path}/{name} rows={}\n{:#?}", t.count_rows(None).await?, s.fields().iter().map(|f| format!("{}: {}", f.name(), f.data_type())).collect::<Vec<_>>());
            println!("indices: {:?}", t.list_indices().await?.iter().map(|i| (&i.name, &i.columns, format!("{:?}", i.index_type))).collect::<Vec<_>>());
            let b: Vec<_> = t.query().limit(2).execute().await?.try_collect().await?;
            if let Some(batch) = b.first() { show(batch); }
        }
    }
    Ok(())
}

fn show(b: &lancedb::arrow::arrow_array::RecordBatch) {
    use lancedb::arrow::arrow_array::Array;
    for (i, f) in b.schema().fields().iter().enumerate() {
        let c = b.column(i);
        let v = lancedb::arrow::arrow_cast::display::ArrayFormatter::try_new(c.as_ref(), &Default::default()).map(|fm| (0..c.len().min(2)).map(|r| { let s = fm.value(r).to_string(); s.chars().take(200).collect::<String>() }).collect::<Vec<_>>());
        println!("  {}: {:?}", f.name(), v.unwrap_or_default());
    }
}
