use rusqlite::{Connection, OpenFlags, Transaction, TransactionBehavior};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

const MAX_IDLE: usize = 4;

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("{path}: {source}")]
    Io { path: PathBuf, source: std::io::Error },
    #[error("{path} is at schema v{found}, newer than this binary's v{ours}: refusing to open it")]
    VersionAhead { path: PathBuf, found: i64, ours: i64 },
    #[error("no migration from v{from} to v{to}")]
    NoMigration { from: i64, to: i64 },
    #[error("backup of {path} before migrating from v{from} failed: {source}")]
    Backup { path: PathBuf, from: i64, source: rusqlite::Error },
    #[error("column {column} holds a blob, which has no JSON form")]
    Blob { column: String },
    #[error("the blocking task failed: {0}")]
    Join(String),
}

/// A pooled SQLite database at one path: WAL, `synchronous=NORMAL`, a 5 s busy timeout and
/// foreign keys on, as the Python services open theirs.
///
/// Idle connections stay open, so a caller's close is never the last one: SQLite checkpoints
/// and fsyncs a WAL database when its last connection closes. A file replaced on disk (a new
/// inode, as a restore leaves) drops the idle connections, so none pins the old file.
#[derive(Clone)]
pub struct Store {
    inner: Arc<Inner>,
}

struct Inner {
    path: PathBuf,
    read_only: bool,
    idle: Mutex<Idle>,
}

#[derive(Default)]
struct Idle {
    inode: Option<u64>,
    conns: Vec<Connection>,
}

impl Store {
    /// Open the database at `path`, creating the file and its directory when absent.
    pub fn open(path: impl AsRef<Path>) -> Result<Store, StoreError> {
        let path = path.as_ref().to_path_buf();
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).map_err(|source| StoreError::Io { path: dir.into(), source })?;
        }
        let store = Store::new(path, false);
        store.release(store.checkout()?);
        Ok(store)
    }

    /// Open an existing database for reading only. Writes fail.
    pub fn open_ro(path: impl AsRef<Path>) -> Result<Store, StoreError> {
        let store = Store::new(path.as_ref().to_path_buf(), true);
        store.release(store.checkout()?);
        Ok(store)
    }

    fn new(path: PathBuf, read_only: bool) -> Store {
        Store { inner: Arc::new(Inner { path, read_only, idle: Mutex::default() }) }
    }

    pub fn path(&self) -> &Path {
        &self.inner.path
    }

    /// A new connection outside the pool, set up as the pool's are. The caller owns it.
    pub fn connection(&self) -> Result<Connection, StoreError> {
        let conn = if self.inner.read_only {
            Connection::open_with_flags(&self.inner.path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?
        } else {
            let conn = Connection::open(&self.inner.path)?;
            conn.pragma_update(None, "journal_mode", "WAL")?;
            conn.pragma_update(None, "synchronous", "NORMAL")?;
            conn
        };
        conn.pragma_update(None, "busy_timeout", 5000)?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        Ok(conn)
    }

    fn inode(&self) -> Option<u64> {
        std::fs::metadata(&self.inner.path).ok().map(|m| m.ino())
    }

    fn checkout(&self) -> Result<Connection, StoreError> {
        let inode = self.inode();
        {
            let mut idle = self.inner.idle.lock().expect("store pool lock");
            if idle.inode != inode {
                idle.conns.clear();
                idle.inode = inode;
            }
            if let Some(conn) = idle.conns.pop() {
                return Ok(conn);
            }
        }
        self.connection()
    }

    fn release(&self, conn: Connection) {
        if !conn.is_autocommit() {
            return;
        }
        let inode = self.inode();
        let mut idle = self.inner.idle.lock().expect("store pool lock");
        if idle.inode.is_none() {
            idle.inode = inode;
        }
        if idle.inode == inode && idle.conns.len() < MAX_IDLE {
            idle.conns.push(conn);
        }
    }

    /// Run `f` on a pooled connection, on this thread.
    pub fn blocking_read<R, E>(&self, f: impl FnOnce(&Connection) -> Result<R, E>) -> Result<R, E>
    where
        E: From<StoreError>,
    {
        let conn = self.checkout()?;
        let out = f(&conn);
        self.release(conn);
        out
    }

    /// Run `f` in a `BEGIN IMMEDIATE` transaction on this thread. It commits when `f` returns
    /// `Ok` and rolls back otherwise.
    pub fn blocking_write<R, E>(&self, f: impl FnOnce(&Transaction) -> Result<R, E>) -> Result<R, E>
    where
        E: From<StoreError>,
    {
        let mut conn = self.checkout()?;
        let out = (|| {
            let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate).map_err(StoreError::from)?;
            let out = f(&tx)?;
            tx.commit().map_err(StoreError::from)?;
            Ok(out)
        })();
        self.release(conn);
        out
    }

    /// [`Store::blocking_read`] on tokio's blocking pool.
    pub async fn read<R, E, F>(&self, f: F) -> Result<R, E>
    where
        F: FnOnce(&Connection) -> Result<R, E> + Send + 'static,
        R: Send + 'static,
        E: From<StoreError> + Send + 'static,
    {
        let store = self.clone();
        tokio::task::spawn_blocking(move || store.blocking_read(f))
            .await
            .map_err(|e| StoreError::Join(e.to_string()))?
    }

    /// [`Store::blocking_write`] on tokio's blocking pool.
    pub async fn write<R, E, F>(&self, f: F) -> Result<R, E>
    where
        F: FnOnce(&Transaction) -> Result<R, E> + Send + 'static,
        R: Send + 'static,
        E: From<StoreError> + Send + 'static,
    {
        let store = self.clone();
        tokio::task::spawn_blocking(move || store.blocking_write(f))
            .await
            .map_err(|e| StoreError::Join(e.to_string()))?
    }
}
