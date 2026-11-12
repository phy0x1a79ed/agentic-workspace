use super::*;
use baitan_provider::backend::Done;
use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use tokio::sync::mpsc::UnboundedReceiver;

struct Rig {
    dir: tempfile::TempDir,
    cfg: SlurmConfig,
    be: SlurmBackend,
    rx: UnboundedReceiver<Done>,
}

const SBATCH: &str = r#"
echo "sbatch $*" >> $D/calls.log
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
            "echo \"squeue $*\" >> $D/calls.log\nfor f in $D/q/*; do [ -e \"$f\" ] && kill -0 $(cat $f) 2>/dev/null && echo \"$(basename $f) RUNNING\"; done; true",
        ),
        ("sshare", "echo \"sshare $*\" >> $D/calls.log\ncat $D/share.txt"),
        ("sacct", "echo \"sacct $*\" >> $D/calls.log"),
        ("scancel", "echo \"scancel $*\" >> $D/calls.log"),
    ] {
        let p = bin.join(name);
        std::fs::write(&p, format!("#!/bin/sh\nD={d}\n{body}\n")).unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
    let cfg = SlurmConfig {
        cluster: "fir".into(),
        account: "acct".into(),
        partition: Some("cpu".into()),
        walltime: "01:00:00".into(),
        scratch: dir.path().join("scratch"),
        bin_dir: Some(bin),
    };
    Rig { be: SlurmBackend::new(cfg.clone(), tx), cfg, dir, rx }
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
        std::fs::create_dir_all(&workdir).unwrap();
        std::fs::write(workdir.join("run.sh"), script).unwrap();
        JobSpec { promise: name.into(), workdir, res: Resources { cpus, mem_mb } }
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
async fn burst_makes_at_most_one_call_per_minute() {
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
    assert!(r.count("sbatch") == 3);
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
    for flag in ["--array=0-4", "--cpus-per-task=2", "--mem=1024M", "--account=acct", "--partition=cpu", "--time=01:00:00"] {
        assert!(sb[0].contains(flag), "{flag} missing in {}", sb[0]);
    }
}

#[tokio::test(start_paused = true)]
async fn two_profiles_make_two_sbatches_a_minute_apart() {
    let mut r = rig();
    for (i, (c, m)) in [(1, 512), (1, 512), (4, 8192)].into_iter().enumerate() {
        let j = r.job(&format!("j{i}"), c, m, "exit 0");
        r.be.launch(j).await.unwrap();
    }
    for _ in 0..3 {
        r.done().await;
    }
    let sb: Vec<_> = r.be.calls().into_iter().filter(|c| c.1.starts_with("sbatch")).collect();
    assert_eq!(sb.len(), 2);
    assert!(sb[1].0 - sb[0].0 >= GATE);
    assert!(sb[0].1.contains("--array=0-1") && sb[1].1.contains("--array=0-0"));
    assert!(sb[1].1.contains("--cpus-per-task=4") && sb[1].1.contains("--mem=8192M"));
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
    sleep(EXIT_POLL * 3).await;
    assert!(r.rx.try_recv().is_err());
}

#[tokio::test(start_paused = true)]
async fn eta_rises_with_worse_fair_share_and_includes_batching_wait() {
    let mut r = rig();
    let res = Resources { cpus: 1, mem_mb: 1024 };
    let share = |r: &Rig, v: &str| std::fs::write(r.dir.path().join("share.txt"), v).unwrap();

    // Batching wait: two other profiles queued ahead add two gate periods.
    let empty = r.be.start_eta(&res).await;
    for (i, c) in [2u32, 3].into_iter().enumerate() {
        let j = r.job(&format!("q{i}"), c, 1024, "exit 0");
        r.be.launch(j).await.unwrap();
    }
    let loaded = r.be.start_eta(&res).await;
    assert_eq!(loaded - empty, 2.0 * GATE.as_secs_f64());
    for _ in 0..2 {
        r.done().await;
    }

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
async fn life_reports_cached_state_and_exit() {
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
    // The task script is killed before it can write `exit`, as Slurm does at walltime.
    let a = r.job("lost", 1, 1024, "kill -9 $PPID");
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
async fn restarted_front_recovers_persisted_jobs() {
    let r = rig();
    let x = r.job("x", 1, 1024, "sleep 1000");
    r.be.launch(x).await.unwrap();
    while r.be.life("x").await["state"] == "queued" {
        sleep(Duration::from_secs(1)).await;
    }
    let path = r.cfg.scratch.join("fir/jobs.json");
    let mut saved: Vec<Saved> = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    assert_eq!(saved.len(), 1);
    assert_eq!(saved[0].promise, "x");

    // The front dies; two more tasks ended meanwhile, one with an exit file and one without.
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    drop(r.be);
    let done = r.dir.path().join("work/done");
    let lost = r.dir.path().join("work/lost");
    std::fs::create_dir_all(&done).unwrap();
    std::fs::create_dir_all(&lost).unwrap();
    std::fs::write(done.join("exit"), "5\n").unwrap();
    saved.push(Saved { key: "900_0".into(), promise: "done".into(), workdir: done });
    saved.push(Saved { key: "900_1".into(), promise: "lost".into(), workdir: lost });
    std::fs::write(&path, serde_json::to_vec(&saved).unwrap()).unwrap();

    let _be = SlurmBackend::new(r.cfg.clone(), tx);
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
}
