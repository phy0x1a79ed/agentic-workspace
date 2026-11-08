//! Typed cells between JSON rows, Arrow batches and hits.

use awm_pycompat::time::{fromisoformat, zulu, DateTime};
use indexmap::IndexMap;
use lancedb::arrow::arrow_array::builder::{
    FixedSizeListBuilder, Float32Builder, Float64Builder, Int32Builder, Int64Builder, ListBuilder, StringBuilder,
    TimestampMicrosecondBuilder,
};
use lancedb::arrow::arrow_array::cast::AsArray;
use lancedb::arrow::arrow_array::types::{
    Float32Type, Float64Type, Int32Type, Int64Type, TimestampMicrosecondType, UInt64Type,
};
use lancedb::arrow::arrow_array::{Array, ArrayRef, RecordBatch};
use lancedb::arrow::arrow_schema::{DataType, Field as ArrowField, Schema, SchemaRef, TimeUnit};
use serde_json::{Map, Value};
use std::sync::Arc;
use awm_pycompat::repr::py_str;

/// How a column is stored.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Kind {
    Text,
    Texts,
    Float,
    Int,
    Int32,
    /// A UTC instant in microseconds.
    Time,
    /// A fixed-size f32 vector.
    Vector(i32),
}

impl Kind {
    pub fn data_type(self) -> DataType {
        match self {
            Kind::Text => DataType::Utf8,
            Kind::Texts => DataType::List(Arc::new(ArrowField::new("item", DataType::Utf8, true))),
            Kind::Float => DataType::Float64,
            Kind::Int => DataType::Int64,
            Kind::Int32 => DataType::Int32,
            Kind::Time => DataType::Timestamp(TimeUnit::Microsecond, Some("UTC".into())),
            Kind::Vector(dim) => DataType::FixedSizeList(Arc::new(ArrowField::new("item", DataType::Float32, true)), dim),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Cell {
    Null,
    Text(String),
    Texts(Vec<String>),
    Float(f64),
    Int(i64),
    Time(i64),
    Vector(Vec<f32>),
}

impl Cell {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Cell::Text(s) => Some(s),
            _ => None,
        }
    }

    /// The value as a hit shows it: an instant as `%Y-%m-%dT%H:%M:%SZ`.
    pub fn to_json(&self) -> Value {
        match self {
            Cell::Null => Value::Null,
            Cell::Text(s) => Value::String(s.clone()),
            Cell::Texts(v) => Value::Array(v.iter().cloned().map(Value::String).collect()),
            Cell::Float(f) => serde_json::Number::from_f64(*f).map(Value::Number).unwrap_or(Value::Null),
            Cell::Int(i) => Value::from(*i),
            Cell::Time(us) => Value::String(zulu(&DateTime::from_utc_us(*us))),
            Cell::Vector(v) => Value::Array(v.iter().map(|x| Value::from(*x as f64)).collect()),
        }
    }
}

/// `parse_ts`: an ISO string as UTC microseconds, a naive one read as UTC.
pub fn parse_ts(value: &Value) -> Option<i64> {
    let s = value.as_str()?.trim();
    if s.is_empty() {
        return None;
    }
    fromisoformat(s).map(|dt| dt.utc_us())
}

/// `_cell`: a caller's value coerced to the column's kind.
pub fn coerce(kind: Kind, value: Option<&Value>) -> Cell {
    let value = value.unwrap_or(&Value::Null);
    match kind {
        Kind::Text => match value {
            Value::Null => Cell::Null,
            v => Cell::Text(py_str(v)),
        },
        Kind::Texts => {
            let items: Vec<&Value> = match value {
                Value::Array(a) => a.iter().collect(),
                Value::Null => vec![],
                Value::String(s) if s.is_empty() => vec![],
                v => vec![v],
            };
            Cell::Texts(
                items
                    .into_iter()
                    .filter(|x| !x.is_null() && x.as_str() != Some(""))
                    .map(py_str)
                    .collect(),
            )
        }
        Kind::Float => match value {
            Value::Number(n) => n.as_f64().map(Cell::Float).unwrap_or(Cell::Null),
            _ => Cell::Null,
        },
        Kind::Int | Kind::Int32 => value.as_i64().map(Cell::Int).unwrap_or(Cell::Null),
        Kind::Time => parse_ts(value).map(Cell::Time).unwrap_or(Cell::Null),
        Kind::Vector(_) => match value {
            Value::Array(a) => Cell::Vector(a.iter().map(|x| x.as_f64().unwrap_or(0.0) as f32).collect()),
            _ => Cell::Null,
        },
    }
}

pub fn schema(cols: &[(String, Kind)]) -> SchemaRef {
    Arc::new(Schema::new(
        cols.iter().map(|(n, k)| ArrowField::new(n, k.data_type(), true)).collect::<Vec<_>>(),
    ))
}

fn column(kind: Kind, cells: &mut dyn Iterator<Item = &Cell>) -> ArrayRef {
    match kind {
        Kind::Text => {
            let mut b = StringBuilder::new();
            for c in cells {
                b.append_option(c.as_str());
            }
            Arc::new(b.finish())
        }
        Kind::Texts => {
            let mut b = ListBuilder::new(StringBuilder::new());
            for c in cells {
                if let Cell::Texts(v) = c {
                    for s in v {
                        b.values().append_value(s);
                    }
                    b.append(true);
                } else {
                    b.append(true);
                }
            }
            Arc::new(b.finish())
        }
        Kind::Float => {
            let mut b = Float64Builder::new();
            for c in cells {
                b.append_option(if let Cell::Float(f) = c { Some(*f) } else { None });
            }
            Arc::new(b.finish())
        }
        Kind::Int => {
            let mut b = Int64Builder::new();
            for c in cells {
                b.append_option(if let Cell::Int(i) = c { Some(*i) } else { None });
            }
            Arc::new(b.finish())
        }
        Kind::Int32 => {
            let mut b = Int32Builder::new();
            for c in cells {
                b.append_option(if let Cell::Int(i) = c { Some(*i as i32) } else { None });
            }
            Arc::new(b.finish())
        }
        Kind::Time => {
            let mut b = TimestampMicrosecondBuilder::new().with_timezone("UTC");
            for c in cells {
                b.append_option(if let Cell::Time(t) = c { Some(*t) } else { None });
            }
            Arc::new(b.finish())
        }
        Kind::Vector(dim) => {
            let mut b = FixedSizeListBuilder::new(Float32Builder::new(), dim);
            for c in cells {
                match c {
                    Cell::Vector(v) => {
                        b.values().append_slice(v);
                        b.append(true);
                    }
                    _ => {
                        b.values().append_nulls(dim as usize);
                        b.append(false);
                    }
                }
            }
            Arc::new(b.finish())
        }
    }
}

/// One batch of `rows`, each holding a cell per column of `cols`.
pub fn batch(cols: &[(String, Kind)], rows: &[Vec<Cell>]) -> crate::Result<RecordBatch> {
    let arrays = cols
        .iter()
        .enumerate()
        .map(|(i, (_, k))| column(*k, &mut rows.iter().map(|r| &r[i])))
        .collect::<Vec<_>>();
    Ok(RecordBatch::try_new(schema(cols), arrays)?)
}

fn cell(array: &dyn Array, i: usize) -> Cell {
    if array.is_null(i) {
        return Cell::Null;
    }
    match array.data_type() {
        DataType::Utf8 => Cell::Text(array.as_string::<i32>().value(i).to_string()),
        DataType::LargeUtf8 => Cell::Text(array.as_string::<i64>().value(i).to_string()),
        DataType::List(_) => {
            let items = array.as_list::<i32>().value(i);
            let s = items.as_string::<i32>();
            Cell::Texts((0..s.len()).filter(|&j| !s.is_null(j)).map(|j| s.value(j).to_string()).collect())
        }
        DataType::Float64 => Cell::Float(array.as_primitive::<Float64Type>().value(i)),
        DataType::Float32 => Cell::Float(array.as_primitive::<Float32Type>().value(i) as f64),
        DataType::Int64 => Cell::Int(array.as_primitive::<Int64Type>().value(i)),
        DataType::Int32 => Cell::Int(array.as_primitive::<Int32Type>().value(i) as i64),
        DataType::UInt64 => Cell::Int(array.as_primitive::<UInt64Type>().value(i) as i64),
        DataType::Timestamp(TimeUnit::Microsecond, _) => Cell::Time(array.as_primitive::<TimestampMicrosecondType>().value(i)),
        DataType::FixedSizeList(_, _) => {
            let v = array.as_fixed_size_list().value(i);
            Cell::Vector(v.as_primitive::<Float32Type>().values().to_vec())
        }
        _ => Cell::Null,
    }
}

pub type Row = IndexMap<String, Cell>;

/// Every row of `batches`, each column by name in batch order.
pub fn rows(batches: &[RecordBatch]) -> Vec<Row> {
    let mut out = Vec::new();
    for b in batches {
        let schema = b.schema();
        for i in 0..b.num_rows() {
            out.push(
                schema
                    .fields()
                    .iter()
                    .zip(b.columns())
                    .map(|(f, a)| (f.name().clone(), cell(a.as_ref(), i)))
                    .collect(),
            );
        }
    }
    out
}

/// A row as JSON, leaving out the columns `skip` names.
pub fn to_map(row: &Row, skip: &[&str]) -> Map<String, Value> {
    row.iter().filter(|(k, _)| !skip.contains(&k.as_str())).map(|(k, v)| (k.clone(), v.to_json())).collect()
}

pub fn text(row: &Row, name: &str) -> String {
    row.get(name).and_then(Cell::as_str).unwrap_or("").to_string()
}
