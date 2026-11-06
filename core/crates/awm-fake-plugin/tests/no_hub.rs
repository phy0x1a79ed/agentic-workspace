//! This test also makes `cargo test --workspace` build the fake plugin's binary for awm-cli's e2e test.

#[test]
fn refuses_to_start_without_a_hub() {
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_awm-fake-plugin"))
        .env_remove("AWM_HUB_URL")
        .output()
        .unwrap();
    assert!(!out.status.success());
}
