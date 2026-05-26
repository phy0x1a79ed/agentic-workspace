use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Frame {
    Exec {
        id: u64,
        cmd: String,
    },
    Stdin {
        id: u64,
        #[serde(with = "b64")]
        data: Vec<u8>,
    },
    StdinClose {
        id: u64,
    },
    Stdout {
        id: u64,
        #[serde(with = "b64")]
        data: Vec<u8>,
    },
    Stderr {
        id: u64,
        #[serde(with = "b64")]
        data: Vec<u8>,
    },
    Exit {
        id: u64,
        code: Option<i32>,
        signal: Option<i32>,
    },
    Error {
        id: u64,
        message: String,
    },
}

impl Frame {
    pub fn to_json(&self) -> serde_json::Result<String> {
        serde_json::to_string(self)
    }

    pub fn from_json(s: &str) -> serde_json::Result<Self> {
        serde_json::from_str(s)
    }
}

mod b64 {
    use base64::{engine::general_purpose::STANDARD, Engine as _};
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(bytes: &Vec<u8>, ser: S) -> Result<S::Ok, S::Error> {
        ser.serialize_str(&STANDARD.encode(bytes))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(de: D) -> Result<Vec<u8>, D::Error> {
        let s = String::deserialize(de)?;
        STANDARD.decode(s.as_bytes()).map_err(serde::de::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(f: Frame) {
        let json = f.to_json().unwrap();
        let parsed = Frame::from_json(&json).unwrap();
        assert_eq!(f, parsed);
    }

    #[test]
    fn exec_roundtrip() {
        roundtrip(Frame::Exec {
            id: 1,
            cmd: "echo hi".into(),
        });
    }

    #[test]
    fn stdout_roundtrip() {
        roundtrip(Frame::Stdout {
            id: 2,
            data: b"hello\n".to_vec(),
        });
    }

    #[test]
    fn stdout_non_utf8_bytes_preserved() {
        let bytes = vec![0xff, 0x00, 0x01, 0xfe, 0x80];
        let f = Frame::Stdout {
            id: 3,
            data: bytes.clone(),
        };
        let json = f.to_json().unwrap();
        let parsed = Frame::from_json(&json).unwrap();
        match parsed {
            Frame::Stdout { data, .. } => assert_eq!(data, bytes),
            _ => panic!("wrong variant"),
        }
    }

    #[test]
    fn stderr_roundtrip() {
        roundtrip(Frame::Stderr {
            id: 4,
            data: b"err\n".to_vec(),
        });
    }

    #[test]
    fn exit_with_code() {
        roundtrip(Frame::Exit {
            id: 5,
            code: Some(7),
            signal: None,
        });
    }

    #[test]
    fn exit_with_signal() {
        roundtrip(Frame::Exit {
            id: 6,
            code: None,
            signal: Some(15),
        });
    }

    #[test]
    fn error_roundtrip() {
        roundtrip(Frame::Error {
            id: 7,
            message: "spawn failed: ENOENT".into(),
        });
    }

    #[test]
    fn stdin_close_roundtrip() {
        roundtrip(Frame::StdinClose { id: 8 });
    }

    #[test]
    fn stdin_roundtrip() {
        roundtrip(Frame::Stdin {
            id: 9,
            data: b"input\n".to_vec(),
        });
    }

    #[test]
    fn json_field_layout() {
        let f = Frame::Exec {
            id: 42,
            cmd: "ls".into(),
        };
        let json = f.to_json().unwrap();
        assert!(json.contains(r#""type":"exec""#));
        assert!(json.contains(r#""id":42"#));
        assert!(json.contains(r#""cmd":"ls""#));
    }
}
