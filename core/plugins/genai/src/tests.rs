use crate::store::Store;

#[tokio::test]
async fn jobs_run_in_order_and_a_restart_fails_the_running_one() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("genai.db");
    let s = Store::open(&path).unwrap();
    s.add("j1", "rt-sticker", "muse-image", "add glasses", &["/in/a.png".into()]).await.unwrap();
    s.add("j2", "rt-sticker", "muse-image", "wave", &[]).await.unwrap();
    s.add("v1", "narrator", "elevenlabs-voice", "hello", &[]).await.unwrap();

    let first = s.claim("muse-image").await.unwrap().unwrap();
    assert_eq!((first.id.as_str(), first.state.as_str(), first.inputs.clone()), ("j1", "running", vec!["/in/a.png".to_string()]));
    assert!(s.finish("j1", &["running"], "done", "", &["/out/j1.png".into()]).await.unwrap());
    assert!(!s.finish("j1", &["running"], "failed", "late", &[]).await.unwrap(), "a finished job does not move again");

    assert_eq!(s.claim("muse-image").await.unwrap().unwrap().id, "j2");
    assert!(s.finish("j2", &["queued", "running"], "cancelled", "by caller", &[]).await.unwrap());
    assert!(!s.finish("j2", &["running"], "done", "", &["/out/j2.png".into()]).await.unwrap(), "a cancelled job's result is dropped");
    assert!(s.claim("muse-image").await.unwrap().is_none());

    assert_eq!(s.claim("elevenlabs-voice").await.unwrap().unwrap().id, "v1");
    drop(s);
    let s = Store::open(&path).unwrap();
    let v1 = s.get("v1").await.unwrap().unwrap();
    assert_eq!((v1.state.as_str(), v1.error.as_str()), ("failed", "genai restarted while the job ran"));
    assert_eq!(s.get("j1").await.unwrap().unwrap().outputs, ["/out/j1.png"]);
    assert_eq!(s.recent(10, Some("done".into())).await.unwrap().len(), 1);
}
