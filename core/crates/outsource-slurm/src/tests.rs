use super::*;
use outsource_provider::backend::Done;
use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use tokio::sync::mpsc::UnboundedReceiver;
use tokio::time::sleep;

struct Rig {
    dir: tempfile::TempDir,
    cfg: SlurmConfig,
    saved: PathBuf,
    be: SlurmBackend,
    rx: UnboundedReceiver<Done>,
}

const SBATCH: &str = r#"
echo "sbatch $*" >> $D/calls.log
case "$*" in *--cpus-per-task=99\ *) echo "sbatch: error: invalid cpus" >&2; exit 1;; esac
n=$(cat $D/jobid 2>/dev/null || echo 100); n=$((n+1)); echo $n > $D/jobid
for a; do case $a in --array=*) r=${a#--array=};; esac; script=$a; done
i=0
while [ $i -le ${r#0-} ]; do
  SLURM_ARRAY_TASK_ID=$i sh "$script" >/dev/null 2>&1 </dev/null &
  mkdir -p $D/q; echo $! > $D/q/${n}_$i
  i=$((i+1))
done
echo "Submitted batch job $n"
"#;

fn rig() -> Rig {
    let dir = tempfile::tempdir().unwrap();
    let bin = dir.path().join("bin");
    std::fs::create_dir(&bin).unwrap();
    let d = dir.path().display();
    std::fs::write(dir.path().join("share.txt"), "0.5\n").unwrap();
    for (name, body) in [
        ("sbatch", SBATCH),
        (
            "squeue",
            "echo \"squeue $*\" >> $D/calls.log\nfor f in $D/q/*; do [ -e \"$f\" ] || continue; p=$(cat $f); grep -q \"^State:.[RSD]\" /proc/$p/status 2>/dev/null && echo \"$(basename $f) RUNNING\"; done; true",
        ),
        ("sshare", "echo \"sshare $*\" >> $D/calls.log\ncat $D/share.txt"),
        ("sacct", "echo \"sacct $*\" >> $D/calls.log"),
        ("scancel", "echo \"scancel $*\" >> $D/calls.log"),
    ] {
        let p = bin.join(name);
        // `down` stands for a dead ssh master: every call fails.
        std::fs::write(&p, format!("#!/bin/sh\nD={d}\n[ -e $D/down ] && exit 255\n{body}\n")).unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
    let cfg = SlurmConfig {
        account: "acct".into(),
        partition: Some("cpu".into()),
        walltime: "05:00:00".into(),
        scratch: dir.path().join("scratch"),
        bin_dir: Some(bin),
        whole_node: false,
        node_cpus: None,
    };
    let saved = dir.path().join("state/jobs.json");
    Rig { be: SlurmBackend::new(cfg.clone(), Host::default(), saved.clone(), tx), cfg, saved, dir, rx }
}

impl Rig {
    fn log(&self) -> Vec<String> {
        std::fs::read_to_string(self.dir.path().join("calls.log"))
            .unwrap_or_default()
            .lines()
            .map(String::from)
            .collect()
    }
    fn count(&self, cmd: &str) -> usize {
        self.log().iter().filter(|l| l.starts_with(cmd)).count()
    }
    fn job(&self, name: &str, cpus: u32, mem_mb: u64, script: &str) -> JobSpec {
        let workdir = self.dir.path().join("work").join(name);
        JobSpec { promise: name.into(), workdir, script: script.into(), res: Resources { cpus, mem_mb }, est_s: None }
    }
    async fn done(&mut self) -> Done {
        self.rx.recv().await.unwrap()
    }
}

/// Virtual-time wait (plain `recv` is also safe: with no timer pending the runtime blocks on I/O). Real subprocesses finish in real time while paused time auto-advances,
/// so tests assert only lower bounds on gaps, taken from the backend's own recorded call times.
async fn until(mut f: impl FnMut() -> bool) {
    for _ in 0..200_000 {
        if f() {
            return;
        }
        sleep(Duration::from_secs(1)).await;
    }
    panic!("condition never held");
}

fn assert_gated(calls: &[(Instant, String)]) {
    for w in calls.windows(2) {
        assert!(w[1].0 - w[0].0 >= GATE, "{} then {} too close", w[0].1, w[1].1);
    }
}

#[tokio::test(start_paused = true)]
async fn burst_makes_one_call_per_slot() {
    let mut r = rig();
    for i in 0..6 {
        let j = r.job(&format!("j{i}"), 1 + (i % 3) as u32, 1024, "exit 0");
        r.be.launch(j).await.unwrap();
        r.be.start_eta(&Resources::default()).await;
        r.be.life(&format!("j{i}")).await;
    }
    for _ in 0..6 {
        r.done().await;
    }
    assert_eq!(r.count("sbatch"), 3);
    let slots: Vec<_> = r.be.calls().into_iter().filter(|c| c.1.starts_with("slot 3 sbatch")).collect();
    assert_eq!(slots.len(), 1, "{:?}", r.be.calls());
    assert_gated(&r.be.calls());
}

#[tokio::test(start_paused = true)]
async fn five_same_profile_launches_make_one_array() {
    let mut r = rig();
    for i in 0..5 {
        let j = r.job(&format!("j{i}"), 2, 1024, "exit 0");
        r.be.launch(j).await.unwrap();
    }
    for _ in 0..5 {
        r.done().await;
    }
    let sb: Vec<_> = r.log().into_iter().filter(|l| l.starts_with("sbatch")).collect();
    assert_eq!(sb.len(), 1, "{sb:?}");
    for flag in ["--array=0-4", "--nodes=1", "--cpus-per-task=2", "--mem=1024M", "--account=acct", "--partition=cpu", "--time=05:00:00"] {
        assert!(sb[0].contains(flag), "{flag} missing in {}", sb[0]);
    }
}

#[tokio::test(start_paused = true)]
async fn two_profiles_make_two_sbatches_in_one_call() {
    let mut r = rig();
    for (i, (c, m)) in [(1, 512), (1, 512), (4, 8192)].into_iter().enumerate() {
        let j = r.job(&format!("j{i}"), c, m, "exit 0");
        r.be.launch(j).await.unwrap();
    }
    for _ in 0..3 {
        r.done().await;
    }
    let sb: Vec<_> = r.log().into_iter().filter(|l| l.starts_with("sbatch")).collect();
    assert_eq!(sb.len(), 2);
    assert!(sb[0].contains("--array=0-1") && sb[1].contains("--array=0-0"));
    assert!(sb[1].contains("--cpus-per-task=4") && sb[1].contains("--mem=8192M"));
    assert_eq!(r.be.calls().iter().filter(|c| c.1.starts_with("slot 2 sbatch")).count(), 1);
}

#[tokio::test(start_paused = true)]
async fn walltime_is_twice_the_estimate_in_whole_hours_capped_at_the_cluster() {
    let mut r = rig();
    let w = |e| r.be.inner.walltime(e);
    assert_eq!((w(Some(1000.0)), w(Some(5000.0)), w(Some(20000.0)), w(None)), ("1:00:00".into(), "3:00:00".into(), "05:00:00".into(), "05:00:00".into()));
    // Different walltimes cannot share an array.
    for (name, est) in [("short", 100.0), ("long", 5000.0), ("short2", 200.0)] {
        let j = JobSpec { est_s: Some(est), ..r.job(name, 1, 1024, "exit 0") };
        r.be.launch(j).await.unwrap();
    }
    for _ in 0..3 {
        r.done().await;
    }
    let sb: Vec<_> = r.log().into_iter().filter(|l| l.starts_with("sbatch")).collect();
    assert_eq!(sb.len(), 2, "{sb:?}");
    assert!(sb[0].contains("--array=0-1") && sb[0].contains("--time=1:00:00"));
    assert!(sb[1].contains("--array=0-0") && sb[1].contains("--time=3:00:00"));
}

#[tokio::test(start_paused = true)]
async fn a_failed_sbatch_fails_only_its_own_jobs() {
    let mut r = rig();
    let (bad, ok) = (r.job("bad", 99, 1024, "exit 0"), r.job("ok", 1, 1024, "exit 0"));
    r.be.launch(bad).await.unwrap();
    r.be.launch(ok).await.unwrap();
    // The first answer is the failed group's, in the slot that submitted both.
    assert_eq!(r.done().await, ("bad".to_string(), -1));
    let error = r.be.inner.st.lock().unwrap().error.clone().unwrap();
    assert!(error.contains("invalid cpus"), "{error}");
    assert_eq!(r.done().await, ("ok".to_string(), 0));
}

#[tokio::test(start_paused = true)]
async fn whole_node_bids_waste_the_idle_cores() {
    let r = rig();
    let whole = |node_cpus| {
        let cfg = SlurmConfig { whole_node: true, node_cpus, ..r.cfg.clone() };
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        SlurmBackend::new(cfg, Host::default(), r.dir.path().join("w.json"), tx)
    };
    let res = Resources { cpus: 48, mem_mb: 1024 };
    assert_eq!(whole(Some(192)).waste(&res), 0.75);
    assert_eq!(whole(Some(192)).waste(&Resources { cpus: 192, mem_mb: 1 }), 0.0);
    assert_eq!(whole(None).waste(&res), 0.0);
    assert_eq!(r.be.waste(&res), 0.0);
}

#[tokio::test(start_paused = true)]
async fn every_job_reports_its_exit_code() {
    let mut r = rig();
    for (name, script) in [("ok", "exit 0"), ("bad", "exit 3"), ("ok2", "echo hi")] {
        let j = r.job(name, 1, 1024, script);
        r.be.launch(j).await.unwrap();
    }
    let mut got = HashMap::new();
    for _ in 0..3 {
        let (p, c) = r.done().await;
        got.insert(p, c);
    }
    assert_eq!(got, HashMap::from([("ok".into(), 0), ("bad".into(), 3), ("ok2".into(), 0)]));
    // Each job reported once.
    sleep(GATE * 3).await;
    assert!(r.rx.try_recv().is_err());
}

#[tokio::test(start_paused = true)]
async fn eta_rises_with_worse_fair_share_and_includes_batching_wait() {
    let r = rig();
    let res = Resources { cpus: 1, mem_mb: 1024 };
    let share = |r: &Rig, v: &str| std::fs::write(r.dir.path().join("share.txt"), v).unwrap();

    // Batching wait: the hold, before any share is known.
    assert_eq!(r.be.start_eta(&res).await, MAX_QUEUE_WAIT * 0.25 + HOLD.as_secs_f64());

    // Fair-share: a worse factor, once the cache refreshes, gives a longer ETA.
    share(&r, "0.9\n");
    sleep(GATE * 2).await;
    let mut good = r.be.start_eta(&res).await;
    while good > 400.0 {
        sleep(Duration::from_secs(1)).await;
        good = r.be.start_eta(&res).await;
    }
    share(&r, "0.1\n");
    sleep(GATE * 2).await;
    let mut bad = r.be.start_eta(&res).await;
    while bad < 1000.0 {
        sleep(Duration::from_secs(1)).await;
        bad = r.be.start_eta(&res).await;
    }
    assert!(bad > good + 1000.0, "good {good} bad {bad}");
}

#[tokio::test(start_paused = true)]
async fn bids_and_life_never_call_slurm_when_the_gate_is_closed() {
    let mut r = rig();
    let j = r.job("a", 1, 1024, "exit 0");
    r.be.launch(j).await.unwrap();
    r.done().await;
    let calls = r.be.calls().len();
    for _ in 0..50 {
        let (n, last) = (r.be.calls().len(), r.be.calls().last().unwrap().0);
        // A bid returns from cache without suspending, so it cannot have made a call.
        r.be.start_eta(&Resources::default()).await;
        r.be.life("a").await;
        assert_eq!(r.be.calls().len(), n, "a bid made a call");
        tokio::task::yield_now().await;
        // Whatever the scheduler then did, the gate held.
        if Instant::now() < last + GATE {
            assert_eq!(r.be.calls().len(), n, "a call inside the closed gate");
        }
    }
    // Once the gate opens, the stale cache refreshes lazily.
    until(|| r.be.calls().len() > calls).await;
    assert_gated(&r.be.calls());
}

#[tokio::test(start_paused = true)]
async fn kill_cancels_locally_before_submit_and_via_scancel_after() {
    let mut r = rig();
    let a = r.job("a", 1, 1024, "exit 0");
    r.be.launch(a).await.unwrap();
    r.be.kill("a").await;
    sleep(GATE * 3).await;
    assert_eq!(r.count("sbatch"), 0);
    assert_eq!(r.count("scancel"), 0);

    let b = r.job("b", 1, 1024, "sleep 1000");
    let c = r.job("c", 1, 1024, "exit 0");
    r.be.launch(b).await.unwrap();
    r.be.launch(c).await.unwrap();
    until(|| r.count("sbatch") == 1).await;
    r.be.kill("b").await;
    until(|| r.count("scancel") == 1).await;
    assert!(r.log().iter().any(|l| l == "scancel 101_0"));
    assert_eq!(r.done().await, ("c".to_string(), 0));
    assert_gated(&r.be.calls());
}

#[tokio::test(start_paused = true)]
async fn life_reports_cached_state() {
    let mut r = rig();
    let j = r.job("a", 1, 1024, "exit 0");
    assert_eq!(r.be.life("a").await["state"], "unknown");
    r.be.launch(j).await.unwrap();
    assert_eq!(r.be.life("a").await["state"], "queued");
    while r.be.life("a").await["state"] == "queued" {
        sleep(Duration::from_secs(1)).await;
    }
    r.be.life("a").await;
    until(|| r.count("squeue") >= 1).await;
    let v = r.be.life("a").await;
    assert!(v["state"] == "RUNNING" || v["state"] == "not-in-queue" || v["state"] == "submitted", "{v}");
    r.done().await;
}

#[tokio::test(start_paused = true)]
async fn vanished_task_without_exit_is_settled_as_failed() {
    let mut r = rig();
    // The task script is killed before it can write `exit`, as Slurm does at walltime. The job
    // finds task.sh among its ancestors, since a `time` wrapper may sit in between.
    let a = r.job(
        "lost",
        1,
        1024,
        "p=$PPID; until grep -aq task.sh /proc/$p/cmdline; do p=$(cut -d' ' -f4 /proc/$p/stat); done; kill -9 $p",
    );
    let b = r.job("fine", 1, 1024, "exit 0");
    r.be.launch(a).await.unwrap();
    r.be.launch(b).await.unwrap();
    let mut got = HashMap::new();
    for _ in 0..2 {
        let (p, c) = r.done().await;
        got.insert(p, c);
    }
    assert_eq!(got, HashMap::from([("lost".into(), -1), ("fine".into(), 0)]));
    assert_gated(&r.be.calls());
}

#[tokio::test(start_paused = true)]
async fn restarted_provider_recovers_persisted_jobs() {
    let r = rig();
    let x = r.job("x", 1, 1024, "sleep 1000");
    r.be.launch(x).await.unwrap();
    while r.be.life("x").await["state"] == "queued" {
        sleep(Duration::from_secs(1)).await;
    }
    let path = r.saved.clone();
    let mut saved: Vec<Saved> = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    assert_eq!(saved.len(), 1);
    assert_eq!(saved[0].promise, "x");

    // The provider dies; two more tasks ended meanwhile, one with an exit file and one without.
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let again = r.job("x", 1, 1024, "sleep 1000");
    drop(r.be);
    let done = r.dir.path().join("work/done");
    let lost = r.dir.path().join("work/lost");
    std::fs::create_dir_all(&done).unwrap();
    std::fs::create_dir_all(&lost).unwrap();
    std::fs::write(done.join("exit"), "5\n").unwrap();
    saved.push(Saved { key: "900_0".into(), promise: "done".into(), workdir: done });
    saved.push(Saved { key: "900_1".into(), promise: "lost".into(), workdir: lost });
    std::fs::write(&path, serde_json::to_vec(&saved).unwrap()).unwrap();

    let _be = SlurmBackend::new(r.cfg.clone(), Host::default(), path, tx);
    // The restarted provider launches its open promise again; it is already submitted.
    _be.launch(again).await.unwrap();
    let mut got = HashMap::new();
    for _ in 0..2 {
        let (p, c) = rx.recv().await.unwrap();
        got.insert(p, c);
    }
    assert_eq!(got, HashMap::from([("done".into(), 5), ("lost".into(), -1)]));
    // x is still alive in the fake queue, so it stays outstanding.
    sleep(GATE * 3).await;
    assert!(rx.try_recv().is_err());
    assert_eq!(_be.life("x").await["state"], "RUNNING");
    let log = std::fs::read_to_string(r.dir.path().join("calls.log")).unwrap();
    assert_eq!(log.lines().filter(|l| l.starts_with("sbatch")).count(), 1);
}

#[tokio::test(start_paused = true)]
async fn a_dead_master_fails_calls_at_the_gate_and_shows_in_life() {
    let mut r = rig();
    // A file where the scratch dir belongs fails every call's `mkdir`, as a dead ssh master would.
    std::fs::write(&r.cfg.scratch, "").unwrap();
    r.be.start_eta(&Resources::default()).await;
    until(|| r.be.calls().len() >= 3).await;
    assert_gated(&r.be.calls());
    let j = r.job("a", 1, 1024, "exit 0");
    r.be.launch(j).await.unwrap();
    assert!(r.be.life("a").await["error"].as_str().is_some_and(|e| !e.is_empty()));
    std::fs::remove_file(&r.cfg.scratch).unwrap();
    assert_eq!(r.done().await, ("a".to_string(), 0));
    assert!(r.be.life("a").await["state"] == "unknown");
    assert_gated(&r.be.calls());
}

/// Prints what each cluster would be sent for a metasmith-shaped burst, through a fake `ssh`
/// that records every script and answers sbatch. No real host is reached.
/// `cargo test -p outsource-slurm mock_submissions -- --ignored --nocapture`
#[tokio::test(start_paused = true)]
#[ignore]
async fn mock_submissions() {
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("ssh");
    std::fs::create_dir(&log).unwrap();
    let fake = dir.path().join("ssh-bin");
    std::fs::create_dir(&fake).unwrap();
    let ssh = fake.join("ssh");
    std::fs::write(
        &ssh,
        format!(
            "#!/bin/sh\nL={}\nn=$(ls $L | wc -l); f=$L/$(printf %03d $n)-$3.sh\necho \"# argv: $*\" > $f; cat >> $f\ngrep -o '#sbatch g[0-9]*' $f | while read h k; do echo \"#sbatch $k Submitted batch job $((9000+n))\"; done\ntrue\n",
            log.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&ssh, std::fs::Permissions::from_mode(0o755)).unwrap();
    std::env::set_var("PATH", format!("{}:{}", fake.display(), std::env::var("PATH").unwrap()));

    let clusters = [
        ("fir", "def-shallam", false),
        ("nibi", "def-shallam", false),
        ("trillium", "def-shallam", true),
        ("sockeye", "st-shallam-1", false),
    ];
    let mut backends = Vec::new();
    for (name, account, whole_node) in clusters {
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let cfg = SlurmConfig {
            account: account.into(),
            partition: None,
            walltime: "02:00:00".into(),
            scratch: format!("/scratch/outsource/{name}").into(),
            bin_dir: None,
            whole_node,
            node_cpus: whole_node.then_some(192),
        };
        let saved = dir.path().join(format!("{name}/jobs.json"));
        backends.push((name, SlurmBackend::new(cfg, Host::new(Some(name.into())), saved, tx), _rx));
    }
    // One msm run split across the four: prodigal per genome (2 cpu, 4 GiB) and a DIAMOND
    // search per protein set (16 cpu, 64 GiB).
    for (c, (name, be, _)) in backends.iter().enumerate() {
        for g in 0..3 {
            let w = format!("/scratch/outsource/{name}/work/prodigal-g{}", c * 3 + g);
            let script = format!("prodigal -i genome_{}.fna -a proteins.faa\n", c * 3 + g);
            be.launch(JobSpec { promise: format!("prodigal-{c}-{g}"), workdir: w.into(), script, res: Resources { cpus: 2, mem_mb: 4096 }, est_s: Some(600.0) })
                .await
                .unwrap();
        }
        let w = format!("/scratch/outsource/{name}/work/diamond-{c}");
        let script = "diamond blastp -d uniref50.dmnd -q proteins.faa -o hits.tsv --threads 16\n".to_string();
        be.launch(JobSpec { promise: format!("diamond-{c}"), workdir: w.into(), script, res: Resources { cpus: 16, mem_mb: 65536 }, est_s: Some(5400.0) })
            .await
            .unwrap();
    }
    // Paused time races ahead of the real ssh subprocesses, so step both clocks together.
    for _ in 0..300 {
        sleep(Duration::from_secs(1)).await;
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
    let mut files: Vec<_> = std::fs::read_dir(&log).unwrap().map(|e| e.unwrap().path()).collect();
    files.sort();
    for f in files {
        println!("===== {}\n{}", f.file_name().unwrap().to_string_lossy(), std::fs::read_to_string(&f).unwrap());
    }
}
