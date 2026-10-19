//! Conversion between JSON and D-Bus `a{sv}` dictionaries.
//!
//! Task params, deliverables and answers travel as `a{sv}` on the bus, and are handled as JSON
//! objects in the API so that Python and other dynamic callers can pass plain dicts.

use std::collections::HashMap;

use serde_json::{Map, Number, Value as Json};
use zbus::{OwnedVariant, Variant};

use crate::{Error, Result};

/// A D-Bus `a{sv}` dictionary.
pub type Dict = HashMap<String, OwnedVariant>;

/// Convert a JSON object into an `a{sv}` dictionary.
pub fn json_to_dict(json: &Json) -> Result<Dict> {
    let Json::Object(map) = json else {
        return Err(Error::Value(format!("expected a JSON object, got {json}")));
    };
    map.iter()
        .map(|(k, v)| Ok((k.clone(), owned(to_variant(v)?)?)))
        .collect()
}

/// Convert an `a{sv}` dictionary into a JSON object.
pub fn dict_to_json(dict: &Dict) -> Result<Json> {
    let mut map = Map::new();
    for (k, v) in dict {
        map.insert(k.clone(), to_json(v)?);
    }
    Ok(Json::Object(map))
}

fn owned(v: Variant<'static>) -> Result<OwnedVariant> {
    OwnedVariant::try_from(v).map_err(|e| Error::Value(e.to_string()))
}

fn to_variant(json: &Json) -> Result<Variant<'static>> {
    Ok(match json {
        Json::Null => return Err(Error::Value("null has no D-Bus representation".into())),
        Json::Bool(b) => Variant::from(*b),
        Json::Number(n) => {
            if let Some(i) = n.as_i64() {
                Variant::from(i)
            } else if let Some(u) = n.as_u64() {
                Variant::from(u)
            } else {
                Variant::from(n.as_f64().expect("a JSON number is i64, u64 or f64"))
            }
        }
        Json::String(s) => Variant::from(s.clone()),
        Json::Array(items) => {
            let items: Vec<Variant<'static>> =
                items.iter().map(to_variant).collect::<Result<_>>()?;
            Variant::from(items)
        }
        Json::Object(map) => {
            let mut dict: HashMap<String, Variant<'static>> = HashMap::new();
            for (k, v) in map {
                dict.insert(k.clone(), to_variant(v)?);
            }
            Variant::from(dict)
        }
    })
}

fn to_json(v: &Variant<'_>) -> Result<Json> {
    Ok(match v {
        Variant::U8(x) => Json::from(*x),
        Variant::Bool(x) => Json::from(*x),
        Variant::I16(x) => Json::from(*x),
        Variant::U16(x) => Json::from(*x),
        Variant::I32(x) => Json::from(*x),
        Variant::U32(x) => Json::from(*x),
        Variant::I64(x) => Json::from(*x),
        Variant::U64(x) => Json::from(*x),
        Variant::F64(x) => Number::from_f64(*x)
            .map(Json::Number)
            .ok_or_else(|| Error::Value(format!("{x} has no JSON representation")))?,
        Variant::Str(s) => Json::from(s.as_str()),
        Variant::Signature(s) => Json::from(s.to_string()),
        Variant::ObjectPath(p) => Json::from(p.as_str()),
        Variant::Variant(inner) => to_json(inner)?,
        Variant::Array(a) => Json::Array(a.inner().iter().map(to_json).collect::<Result<_>>()?),
        Variant::Structure(s) => {
            Json::Array(s.fields().iter().map(to_json).collect::<Result<_>>()?)
        }
        Variant::Dict(d) => {
            let mut map = Map::new();
            for (k, v) in d.iter() {
                let key = match to_json(k)? {
                    Json::String(s) => s,
                    other => other.to_string(),
                };
                map.insert(key, to_json(v)?);
            }
            Json::Object(map)
        }
        #[cfg(unix)]
        Variant::Fd(_) => return Err(Error::Value("file descriptors are not supported".into())),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn round_trip() {
        let v = json!({"s": "x", "i": -3, "u": u64::MAX, "f": 1.5, "b": true,
                       "a": [1, "two"], "o": {"nested": [{"k": 1}]}});
        assert_eq!(dict_to_json(&json_to_dict(&v).unwrap()).unwrap(), v);
    }

    #[test]
    fn rejects_null_and_non_objects() {
        assert!(json_to_dict(&json!({"x": null})).is_err());
        assert!(json_to_dict(&json!([1])).is_err());
    }
}
