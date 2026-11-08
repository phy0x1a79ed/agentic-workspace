use crate::store::StoreError;
use rusqlite::types::ValueRef;
use rusqlite::{Params, Row, Statement};
use serde_json::{Map, Number, Value};

/// A row as a JSON object in column order, as Python's `dict(row)` serialises.
pub fn row_json(row: &Row<'_>) -> Result<Map<String, Value>, StoreError> {
    let stmt = row.as_ref();
    let mut out = Map::with_capacity(stmt.column_count());
    for i in 0..stmt.column_count() {
        let name = stmt.column_name(i)?.to_string();
        let v = match row.get_ref(i)? {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(n) => Value::from(n),
            ValueRef::Real(f) => Number::from_f64(f).map(Value::Number).unwrap_or(Value::Null),
            ValueRef::Text(t) => Value::String(String::from_utf8_lossy(t).into_owned()),
            ValueRef::Blob(_) => return Err(StoreError::Blob { column: name }),
        };
        out.insert(name, v);
    }
    Ok(out)
}

/// Every row of `stmt` as JSON objects.
pub fn rows_json(stmt: &mut Statement<'_>, params: impl Params) -> Result<Vec<Value>, StoreError> {
    let mut rows = stmt.query(params)?;
    let mut out = Vec::new();
    while let Some(row) = rows.next()? {
        out.push(Value::Object(row_json(row)?));
    }
    Ok(out)
}
