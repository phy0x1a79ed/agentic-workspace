//! The genai service: media generation jobs in named styles, over pluggable providers.
//!
//! `generate` queues a job and answers with its id. One worker per provider runs that
//! provider's jobs in order, and `result` returns the files inline, which the caller's gateway
//! writes to its own node.

mod muse;
mod provider;
mod store;
mod styles;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use awm_plugin_sdk::{Call, Level, Service, VerbError};
use base64::Engine;
use serde_json::{json, Value};
use tokio::sync::watch;

use provider::{Provider, Work};
use store::{Job, Store};
use styles::Styles;

const TOOL: &str = "genai";
const DONE: [&str; 3] = ["done", "failed", "cancelled"];
/// The most `generate` waits for its job: inside the default verb budget, and the 130 s a call
/// across a singleton's door gets.
const WAIT_MAX_S: u64 = 100;

struct App {
    store: Store,
    styles: Styles,
    providers: Vec<Arc<dyn Provider>>,
    jobs_dir: PathBuf,
    node: String,
    /// Bumped on each change of a job's state: workers wait on it for work, `generate` for its end.
    changed: watch::Sender<u64>,
}

fn refuse(msg: impl Into<String>) -> VerbError {
    VerbError::value(msg)
}

fn runtime(e: impl std::fmt::Display) -> VerbError {
    VerbError::runtime(e.to_string())
}

/// The `files` the gateway inlined from the caller's paths: `(filename, base64)`.
fn inlined(call: &Call) -> Result<Vec<(String, String)>, VerbError> {
    let Some(files) = call.args.get("files").filter(|f| !f.is_null()) else { return Ok(vec![]) };
    let files = files.as_array().ok_or_else(|| refuse("`files` is a list of paths"))?;
    files
        .iter()
        .enumerate()
        .map(|(i, f)| match (f["filename"].as_str(), f["b64"].as_str()) {
            (Some(n), Some(b)) => Ok((awm_chat::safe_name(n, i), b.to_string())),
            _ => Err(refuse("each of `files` is a local path, which your gateway reads; call genai from a session")),
        })
        .collect()
}

fn job_view(j: &Job) -> Value {
    json!({"id": j.id, "style": j.style, "provider": j.provider, "prompt": j.prompt, "state": j.state, "error": j.error,
        "inputs": j.inputs.len(), "outputs": j.outputs.len(), "created_at": j.created_at, "updated_at": j.updated_at})
}

impl App {
    fn provider(&self, name: &str) -> Result<Arc<dyn Provider>, VerbError> {
        self.providers.iter().find(|p| p.name() == name).cloned().ok_or_else(|| {
            refuse(format!("no provider named {name:?}; genai has {}", self.providers.iter().map(|p| p.name()).collect::<Vec<_>>().join(", ")))
        })
    }

    async fn job(&self, call: &Call) -> Result<Job, VerbError> {
        let id: String = call.req("id")?;
        self.store.get(&id).await.map_err(runtime)?.ok_or_else(|| refuse(format!("no job {id}")))
    }

    async fn generate(&self, call: &Call) -> Result<Value, VerbError> {
        let (name, prompt): (String, String) = (call.req("style")?, call.req("prompt")?);
        let style = self.styles.get(&name).await.map_err(refuse)?;
        let provider = self.provider(&style.provider)?;
        let id = format!("g{}", &uuid::Uuid::new_v4().simple().to_string()[..12]);
        let dir = self.jobs_dir.join(&id);
        std::fs::create_dir_all(&dir).map_err(runtime)?;
        let mut inputs = vec![];
        for (i, (filename, b64)) in inlined(call)?.into_iter().enumerate() {
            let bytes = base64::engine::general_purpose::STANDARD.decode(b64).map_err(|e| refuse(format!("{filename}: {e}")))?;
            let path = dir.join(format!("in{}-{filename}", i + 1));
            std::fs::write(&path, bytes).map_err(runtime)?;
            inputs.push(path.display().to_string());
        }
        self.store.add(&id, &style.name, provider.name(), &prompt, &inputs).await.map_err(runtime)?;
        self.changed.send_modify(|n| *n += 1);
        let wait = call.arg::<f64>("wait")?.unwrap_or(0.0).clamp(0.0, WAIT_MAX_S as f64);
        if wait > 0.0 {
            let mut rx = self.changed.subscribe();
            let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(wait);
            loop {
                let job = self.store.get(&id).await.map_err(runtime)?.ok_or_else(|| runtime("the job vanished"))?;
                if DONE.contains(&job.state.as_str()) || tokio::time::timeout_at(deadline, rx.changed()).await.is_err() {
                    return Ok(json!({"job": job_view(&job), "next": "Fetch the files with `result` when the job is done."}));
                }
            }
        }
        let job = self.store.get(&id).await.map_err(runtime)?.ok_or_else(|| runtime("the job vanished"))?;
        Ok(json!({"job": job_view(&job), "next": "Poll `job` with this id, then fetch the files with `result`."}))
    }

    async fn result(&self, call: &Call) -> Result<Value, VerbError> {
        let job = self.job(call).await?;
        if job.state != "done" {
            return Err(refuse(format!("job {} is {}{}", job.id, job.state, if job.error.is_empty() { String::new() } else { format!(": {}", job.error) })));
        }
        let b64 = base64::engine::general_purpose::STANDARD;
        let mut files = vec![];
        for p in &job.outputs {
            let bytes = std::fs::read(p).map_err(|e| runtime(format!("{p}: {e}")))?;
            let name = p.rsplit('/').next().unwrap_or("out").to_string();
            files.push(json!({"filename": name, "mime": mime_guess::from_path(&name).first_or_octet_stream().to_string(), "size": bytes.len(), "b64": b64.encode(&bytes)}));
        }
        Ok(json!({"files": files, "count": files.len(), "node": self.node}))
    }

    async fn handle(&self, call: Call) -> Result<Value, VerbError> {
        if call.level() == Level::Untrusted && ["generate", "style_add", "cancel"].contains(&call.verb.as_str()) {
            return Err(VerbError::permission(format!("an untrusted session may not {}", call.verb)));
        }
        match call.verb.as_str() {
            "styles" => {
                let list = self.styles.list().await.map_err(runtime)?;
                Ok(json!({"styles": list.iter().map(|s| json!({"name": s.name, "provider": s.provider, "size": s.size, "refs": s.refs.len(), "note_id": s.note_id})).collect::<Vec<_>>()}))
            }
            "style_get" => Ok(json!(self.styles.get(&call.req::<String>("name")?).await.map_err(refuse)?)),
            "style_add" => {
                let (name, provider, text): (String, String, String) = (call.req("name")?, call.req("provider")?, call.req("text")?);
                self.provider(&provider)?;
                let id = self.styles.add(&name, &provider, &text, call.str_arg("size").filter(|s| !s.is_empty()), &inlined(&call)?).await.map_err(refuse)?;
                Ok(json!({"name": name, "note_id": id}))
            }
            "generate" => self.generate(&call).await,
            "job" => Ok(job_view(&self.job(&call).await?)),
            "jobs" => {
                let limit = call.arg::<i64>("limit")?.unwrap_or(20).clamp(1, 200);
                let jobs = self.store.recent(limit, call.str_arg("state").filter(|s| !s.is_empty()).map(str::to_string)).await.map_err(runtime)?;
                Ok(json!({"jobs": jobs.iter().map(job_view).collect::<Vec<_>>()}))
            }
            "result" => self.result(&call).await,
            "cancel" => {
                let job = self.job(&call).await?;
                let moved = self.store.finish(&job.id, &["queued", "running"], "cancelled", "cancelled by the caller", &[]).await.map_err(runtime)?;
                self.changed.send_modify(|n| *n += 1);
                Ok(json!({"id": job.id, "cancelled": moved}))
            }
            "status" => {
                let mut providers = vec![];
                for p in &self.providers {
                    providers.push(json!({"name": p.name(), "kind": p.kind(), "status": p.status().await}));
                }
                Ok(json!({"providers": providers}))
            }
            other => Err(refuse(format!("unknown verb {other}"))),
        }
    }

    async fn run_one(&self, p: &Arc<dyn Provider>, job: Job) {
        let outcome = async {
            let style = self.styles.get(&job.style).await?;
            let refs = self.styles.ref_files(&style).await?;
            let work = Work { dir: self.jobs_dir.join(&job.id), job: job.clone(), style, refs };
            p.run(&work).await
        }
        .await;
        let (state, error, outputs) = match outcome {
            Ok(files) => ("done", String::new(), files.iter().map(|f| f.display().to_string()).collect()),
            Err(e) => ("failed", e, vec![]),
        };
        match self.store.finish(&job.id, &["running"], state, &error, &outputs).await {
            Ok(true) => tracing::info!(job = %job.id, state, %error, "job finished"),
            Ok(false) => tracing::info!(job = %job.id, "job ended while it ran; its result is dropped"),
            Err(e) => tracing::warn!(job = %job.id, "could not record the job's end: {e}"),
        }
        self.changed.send_modify(|n| *n += 1);
    }

    /// Run `p`'s queued jobs one at a time, waking on each change of the queue.
    async fn work(self: Arc<Self>, p: Arc<dyn Provider>) -> anyhow::Result<()> {
        let mut rx = self.changed.subscribe();
        loop {
            while let Some(job) = self.store.claim(p.name()).await? {
                self.run_one(&p, job).await;
            }
            rx.changed().await?;
        }
    }
}

const VERBS: &[(&str, &str, &str)] = &[
    ("styles", "The named styles: each one's provider, output size and number of reference files. A style is a Trilium note under the #genaiStyles folder, which Tony edits by hand.", r#"[]"#),
    ("style_get", "One style: its provider, size, style text and reference files.", r#"[{"name": "name", "type": "string", "required": true, "description": "The style's name."}]"#),
    ("style_add", "Add a style: a Trilium note with the style text, its provider and size, and the reference files attached. To change one, edit its note in Trilium.", r#"[{"name": "name", "type": "string", "required": true, "description": "The style's name, such as rt-sticker."}, {"name": "provider", "type": "string", "required": true, "description": "The provider that renders it, such as muse-image. `status` lists them."}, {"name": "text", "type": "string", "required": true, "description": "The style text sent with every job. Keep it short and let the references carry the look."}, {"name": "size", "type": "string", "description": "The output size, as WxH."}, {"name": "files", "type": "array", "description": "Reference files, by their paths on your node."}]"#),
    ("generate", "Queue a job: the prompt rendered in a style, from optional input files. Answers with the job id at once, or waits up to `wait` seconds for the job to end. A Muse image takes about 2 minutes. Fetch the files with `result`.", r#"[{"name": "style", "type": "string", "required": true, "description": "The style's name. `styles` lists them."}, {"name": "prompt", "type": "string", "required": true, "description": "What to make or change, such as: add glasses in the established style of the character."}, {"name": "files", "type": "array", "description": "Input files to edit, by their paths on your node."}, {"name": "wait", "type": "number", "description": "Seconds to wait for the job to end, at most 100. Default 0. Poll `job` for longer."}]"#),
    ("job", "One job: its state (queued, running, done, failed or cancelled), error and times.", r#"[{"name": "id", "type": "string", "required": true, "description": "The job id `generate` gave."}]"#),
    ("jobs", "The newest jobs first.", r#"[{"name": "limit", "type": "number", "description": "At most this many. Default 20."}, {"name": "state", "type": "string", "description": "Only jobs in this state."}]"#),
    ("result", "A done job's files, written to a fresh folder on your node. The answer names each file's path.", r#"[{"name": "id", "type": "string", "required": true, "description": "The job id."}]"#),
    ("cancel", "Cancel a queued or running job. A running job's provider may still finish it; its result is dropped.", r#"[{"name": "id", "type": "string", "required": true, "description": "The job id."}]"#),
    ("status", "The providers genai serves, and each one's link, such as the Muse room.", r#"[]"#),
];

fn service(svc: Service, app: Arc<App>) -> Service {
    let mut svc = svc.api("description", json!("Generate images and other media in named styles, so new output matches a series. A style is a Trilium note that names its provider (muse-image first), its style text and its reference files. `generate` queues a job and `result` fetches its files to your node."));
    for (name, description, params) in VERBS {
        let app = app.clone();
        svc = svc.verb(TOOL, name, description, serde_json::from_str(params).expect("the verb table's params are JSON"), move |call: Call| {
            let app = app.clone();
            async move { app.handle(call).await }
        });
    }
    svc
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    awm_plugin_sdk::telemetry::init();
    let svc = Service::from_env()?;
    let ctx = svc.context();
    let dir = awm_plugin_sdk::config::service_dir(TOOL);
    let store = Store::open(&dir.join("genai.db"))?;
    let app = Arc::new(App {
        providers: provider::registry(ctx.hub()),
        styles: Styles::new(ctx.hub().clone(), dir.join("refs")),
        store,
        jobs_dir: dir.join("jobs"),
        node: ctx.env().node.clone(),
        changed: watch::channel(0).0,
    });
    let ready = app.clone();
    service(svc, app)
        .on_ready(async move {
            for p in ready.providers.clone() {
                ctx.spawn(&format!("worker {}", p.name()), ready.clone().work(p));
            }
        })
        .run()
        .await
}

#[cfg(test)]
mod tests;
